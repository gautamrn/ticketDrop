package main

import (
	"testing"
	"time"
)

func TestPercentileUsesNearestRank(t *testing.T) {
	values := []float64{1, 2, 3, 4, 5, 6, 7, 8, 9, 10}
	tests := []struct {
		p    float64
		want float64
	}{
		{0.50, 5},
		{0.95, 10},
		{0.99, 10},
	}
	for _, tt := range tests {
		if got := percentile(values, tt.p); got != tt.want {
			t.Errorf("percentile(%v)=%v, want %v", tt.p, got, tt.want)
		}
	}
}

func TestStreamIDTimeUsesRedisMillisecondTimestamp(t *testing.T) {
	got, err := streamIDTime("1731283200123-7")
	if err != nil {
		t.Fatal(err)
	}
	want := time.UnixMilli(1731283200123).UTC()
	if !got.Equal(want) {
		t.Fatalf("streamIDTime=%s, want %s", got, want)
	}
}
