// Package resolve links the same real-world show across two sources.
//
// This exists because the two ingestion tiers do not share an id space. EDMTrain
// tells us a show was announced; RA is where we can watch its sale state. An
// EDMTrain event id is meaningless to RA, so something has to decide that
// "Prospa @ Spot Lite, Sep 12" on one source is the same night as on the other.
//
// The governing bias is that a **false link is worse than no link**. A wrong
// match means we poll some other event and alert on a drop the user cannot buy,
// which is indistinguishable from a bug and destroys trust in every alert. So
// matching requires both an absolute score and a clear margin over the runner-up;
// ambiguity resolves to "no match" and the show simply stays unwatched.
package resolve

import (
	"sort"
	"strings"
	"time"
	"unicode"

	"golang.org/x/text/runes"
	"golang.org/x/text/transform"
	"golang.org/x/text/unicode/norm"
)

// Tunables. These are the decision surface of the whole package, so they live
// together rather than being scattered as literals.
const (
	// AcceptScore is the floor for a link. Set from the observation that a
	// correct match almost always agrees on venue AND lineup, which alone
	// reaches 0.85; requiring 0.60 tolerates one weak signal but not two.
	AcceptScore = 0.60

	// AmbiguityMargin is how far ahead of the runner-up the winner must be.
	// Residencies and festival nights produce near-identical candidates on the
	// same date at the same venue, and picking arbitrarily between them is
	// exactly the false link this package refuses to make.
	AmbiguityMargin = 0.10

	// DateSlack is a ±1 calendar day tolerance expressed in wall clock, which is
	// wider than it first looks and needs to be.
	//
	// EDMTrain gives a date with no time, which we store as midnight UTC. RA
	// gives a real instant. A 22:00 show in Detroit is 02:00 UTC the *next* day,
	// so the same night can be stored 26 h apart before any disagreement between
	// the sources — and a source that lists a late set on the previous evening
	// pushes that to nearly 48 h. A tighter bound silently rejects correct
	// matches for exactly the late-night shows this project cares about.
	//
	// Widening the window admits more candidates rather than more false links:
	// AmbiguityMargin is what rejects a wrong pick, not the date filter.
	DateSlack = 48 * time.Hour

	weightVenue   = 0.45
	weightArtists = 0.40
	weightTitle   = 0.15
)

type Query struct {
	Title     string
	VenueName string
	Date      time.Time
	Artists   []string
}

// Candidate is one event from the target source that might be the same show.
type Candidate struct {
	SourceEventID string
	Title         string
	VenueName     string
	Date          time.Time
	Artists       []string
}

type Match struct {
	Candidate Candidate
	Score     float64
	// Runner-up score, kept so callers can log why a near-miss was rejected.
	RunnerUp float64
}

// Best returns the single best link, or ok == false if nothing clears the bar.
func Best(q Query, candidates []Candidate) (Match, bool) {
	type scored struct {
		c Candidate
		s float64
	}

	var viable []scored
	for _, c := range candidates {
		// Date is a hard filter, not a weighted term. Two different shows at
		// the same venue with overlapping lineups are common; the same show on
		// a different night is not the same show.
		if absDuration(c.Date.Sub(q.Date)) > DateSlack {
			continue
		}
		viable = append(viable, scored{c: c, s: score(q, c)})
	}
	if len(viable) == 0 {
		return Match{}, false
	}

	sort.SliceStable(viable, func(i, j int) bool { return viable[i].s > viable[j].s })

	best := viable[0]
	var runnerUp float64
	if len(viable) > 1 {
		runnerUp = viable[1].s
	}

	if best.s < AcceptScore {
		return Match{Candidate: best.c, Score: best.s, RunnerUp: runnerUp}, false
	}
	if len(viable) > 1 && best.s-runnerUp < AmbiguityMargin {
		return Match{Candidate: best.c, Score: best.s, RunnerUp: runnerUp}, false
	}
	return Match{Candidate: best.c, Score: best.s, RunnerUp: runnerUp}, true
}

// score blends three independent signals so that one missing field degrades the
// result rather than disqualifying a correct match. EDMTrain often has no title
// beyond the lineup, and RA titles frequently carry promoter branding the other
// source never mentions, so title is the weakest term by design.
func score(q Query, c Candidate) float64 {
	var total, used float64

	if q.VenueName != "" && c.VenueName != "" {
		total += weightVenue * venueSimilarity(q.VenueName, c.VenueName)
		used += weightVenue
	}
	if len(q.Artists) > 0 && len(c.Artists) > 0 {
		total += weightArtists * artistOverlap(q.Artists, c.Artists)
		used += weightArtists
	}
	if q.Title != "" && c.Title != "" {
		total += weightTitle * jaccard(tokens(q.Title), tokens(c.Title))
		used += weightTitle
	}

	if used == 0 {
		return 0
	}
	// Renormalize over the signals actually present, so an event with no lineup
	// data is not permanently capped below the accept threshold.
	return total / used
}

// venueSimilarity treats containment as a full match. Sources disagree on venue
// naming conventions constantly — "Spot Lite" vs "Spot Lite Detroit", "fabric"
// vs "fabric London" — and that disagreement is not evidence of a different room.
func venueSimilarity(a, b string) float64 {
	na, nb := normalize(a), normalize(b)
	if na == "" || nb == "" {
		return 0
	}
	if na == nb {
		return 1
	}
	if strings.Contains(na, nb) || strings.Contains(nb, na) {
		return 1
	}
	return jaccard(tokens(a), tokens(b))
}

// artistOverlap uses overlap coefficient rather than Jaccard: a festival lineup
// of thirty acts that includes both of EDMTrain's two listed names is very
// likely the same event, but Jaccard would score that near zero purely because
// the sets differ in size.
func artistOverlap(a, b []string) float64 {
	sa, sb := artistSet(a), artistSet(b)
	if len(sa) == 0 || len(sb) == 0 {
		return 0
	}

	var shared int
	for name := range sa {
		if sb[name] {
			shared++
		}
	}
	smaller := len(sa)
	if len(sb) < smaller {
		smaller = len(sb)
	}
	return float64(shared) / float64(smaller)
}

func artistSet(names []string) map[string]bool {
	set := make(map[string]bool, len(names))
	for _, n := range names {
		if v := normalize(n); v != "" {
			set[v] = true
		}
	}
	return set
}

func jaccard(a, b map[string]bool) float64 {
	if len(a) == 0 || len(b) == 0 {
		return 0
	}
	var shared int
	for t := range a {
		if b[t] {
			shared++
		}
	}
	union := len(a) + len(b) - shared
	if union == 0 {
		return 0
	}
	return float64(shared) / float64(union)
}

// noise words carry no identifying information and appear inconsistently
// between sources, so they would otherwise inflate title similarity between
// unrelated events.
var noise = map[string]bool{
	"the": true, "and": true, "at": true, "in": true, "a": true, "an": true,
	"presents": true, "present": true, "pres": true, "feat": true,
	"featuring": true, "with": true, "w": true, "live": true, "dj": true,
	"open": true, "air": true, "night": true, "party": true, "tour": true,
	"b2b": true, "vs": true, "x": true, "of": true, "for": true, "on": true,
}

func tokens(s string) map[string]bool {
	out := map[string]bool{}
	for _, t := range strings.Fields(normalize(s)) {
		if !noise[t] && len(t) > 1 {
			out[t] = true
		}
	}
	return out
}

// normalize strips diacritics, case, and punctuation. Artist and venue names in
// this scene are full of them — "Haus der Visionäre", "Ø", "Nídia" — and the two
// sources are not consistent about preserving them.
func normalize(s string) string {
	t := transform.Chain(norm.NFD, runes.Remove(runes.In(unicode.Mn)), norm.NFC)
	folded, _, err := transform.String(t, s)
	if err != nil {
		folded = s
	}

	var b strings.Builder
	b.Grow(len(folded))
	lastSpace := true
	for _, r := range strings.ToLower(folded) {
		switch {
		case unicode.IsLetter(r) || unicode.IsDigit(r):
			b.WriteRune(r)
			lastSpace = false
		case !lastSpace:
			b.WriteByte(' ')
			lastSpace = true
		}
	}
	return strings.TrimSpace(b.String())
}

func absDuration(d time.Duration) time.Duration {
	if d < 0 {
		return -d
	}
	return d
}
