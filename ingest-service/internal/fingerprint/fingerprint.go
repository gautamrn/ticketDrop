// Package fingerprint turns an observed availability snapshot into a stable
// hash, and decides whether a change between two snapshots is a drop.
//
// These are two separate jobs on purpose. The hash answers "did anything at all
// change", cheaply, for every event on every poll. Classification answers "is
// that change worth waking someone up for", and only runs on the small subset
// where the hash moved.
package fingerprint

import (
	"crypto/sha256"
	"encoding/hex"
	"strconv"
	"strings"

	"ticketdrop/ingest/internal/source"
)

// Change kinds, matching the change_kind enum in the database.
const (
	ChangeNewTicketed    = "new_ticketed"
	ChangeTierAvailable  = "tier_available"
	ChangeReturnedToSale = "returned_to_sale"
)

const presaleActive = "ACTIVE"
const statusLive = "LIVE"

// Compute hashes the fields that describe sale state.
//
// Deliberately excluded: attending and interestedCount. Those tick constantly
// as people save an event, so including them would make the fingerprint change
// on nearly every poll and destroy its value as a cheap "nothing happened"
// filter. They are demand signals for hype scoring, not availability state.
//
// Deliberately included: dateUpdated. It moves for typo fixes and flyer swaps,
// which are not drops, but it is the only signal that catches a change to a
// field we do not model. A fingerprint delta that Classify cannot explain is
// recorded and dropped rather than alerted on.
func Compute(s source.Snapshot) string {
	var b strings.Builder
	write := func(parts ...string) {
		for _, p := range parts {
			b.WriteString(p)
			b.WriteByte(0x1f) // unit separator: prevents field-boundary collisions
		}
	}

	write(
		strconv.FormatBool(s.IsTicketed),
		normalizeCost(s.Cost),
		strings.ToUpper(strings.TrimSpace(s.PresaleStatus)),
		strconv.FormatBool(s.ResaleActive),
		strconv.FormatBool(s.QueueIt),
		strings.ToUpper(strings.TrimSpace(s.EventStatus)),
	)
	if s.DateUpdated != nil {
		write(s.DateUpdated.UTC().Format("2006-01-02T15:04:05Z"))
	} else {
		write("")
	}

	sum := sha256.Sum256([]byte(b.String()))
	return hex.EncodeToString(sum[:])
}

// normalizeCost collapses whitespace and case so that a cosmetic reformat of
// the same price text does not read as a tier change. RA returns this field as
// free-form stripped HTML, e.g. "£15 - £20" or "15 GBP  advance".
func normalizeCost(cost string) string {
	return strings.ToLower(strings.Join(strings.Fields(cost), " "))
}

// Classify decides what kind of drop a transition represents, if any.
//
// Order matters: a presale opening is the highest-value signal we can observe
// and must win over the cost-text heuristic, which would otherwise also fire
// on the same poll when the price text appears alongside the presale.
//
// Returns ok == false when the fingerprint moved but no rule explains it. That
// is the common case — a flyer swap or a description edit — and it must not
// produce an alert.
func Classify(prev, cur source.Snapshot) (string, bool) {
	switch {
	case !isActive(prev.PresaleStatus) && isActive(cur.PresaleStatus):
		// A presale just opened. This is the transition the whole project
		// exists to catch.
		return ChangeTierAvailable, true

	case !prev.IsTicketed && cur.IsTicketed:
		return ChangeNewTicketed, true

	case !prev.ResaleActive && cur.ResaleActive:
		// Returns went back on sale. Often the only way into a sold-out show.
		return ChangeReturnedToSale, true

	case cur.IsTicketed && normalizeCost(prev.Cost) != normalizeCost(cur.Cost):
		// Price text changed on an already-ticketed event, which is the
		// coarsest proxy we have for a tier being added or repriced. Only
		// meaningful while ticketed: cost text on an unticketed listing is
		// just an announcement detail.
		return ChangeTierAvailable, true

	case !isLive(prev.EventStatus) && isLive(cur.EventStatus) && cur.IsTicketed:
		return ChangeNewTicketed, true
	}
	return "", false
}

func isActive(s string) bool {
	return strings.EqualFold(strings.TrimSpace(s), presaleActive)
}

func isLive(s string) bool {
	return strings.EqualFold(strings.TrimSpace(s), statusLive)
}
