// Posture context builder.
//
// Translates a parsed pose.ergo_risk event + the user's posture history into
// the slim semantic block the posture skill expects as `[posture_context:
// {...}]`. Mirrors the wellbeing/emotion builders in shape, but the agent
// reads raw sub-scores from the message text directly — the context here
// carries only derived booleans + labels (no numbers), per SKILL design.

package skillcontext

import (
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"go-lamp.autonomous.ai/lib/posture"
	"go-lamp.autonomous.ai/lib/usercanon"
)

const postureSubdir = "posture"

// rePostureHeader extracts final score, risk_name, and per-side scores from
// the lelamp pose.ergo_risk message header.
var rePostureHeader = regexp.MustCompile(
	`RULA score (\d+) \(([a-z_]+) risk\)\. Left.*?score=(\d+).*?Right.*?score=(\d+)`,
)

// rePostureArm extracts per-side upper_arm + wrist sub-scores so we can detect
// arm asymmetry that the final side score may flatten (final side score is
// arm+neck+trunk composite; large arm divergence shows up as ≤1 final-side
// delta when neck/trunk dominate the score).
var rePostureArm = regexp.MustCompile(
	`Left.*?upper_arm=(\d+).*?wrist=(\d+).*?Right.*?upper_arm=(\d+).*?wrist=(\d+)`,
)

// ParsePostureMessage extracts the structured header + per-side arm
// sub-scores. Returns the zero value if the header regex misses; arm
// sub-scores default to 0 if their regex misses (still usable for the
// final-side delta path).
func ParsePostureMessage(msg string) PostureEvent {
	m := rePostureHeader.FindStringSubmatch(msg)
	if len(m) != 5 {
		return PostureEvent{}
	}
	score, _ := strconv.Atoi(m[1])
	left, _ := strconv.Atoi(m[3])
	right, _ := strconv.Atoi(m[4])
	ev := PostureEvent{
		Score:      score,
		Risk:       m[2],
		LeftScore:  left,
		RightScore: right,
	}
	if arm := rePostureArm.FindStringSubmatch(msg); len(arm) == 5 {
		ev.LeftUpperArm, _ = strconv.Atoi(arm[1])
		ev.LeftWrist, _ = strconv.Atoi(arm[2])
		ev.RightUpperArm, _ = strconv.Atoi(arm[3])
		ev.RightWrist, _ = strconv.Atoi(arm[4])
	}
	return ev
}

// PostureEvent is the parsed view of a single pose.ergo_risk event. Caller
// (service_events) extracts these from the lelamp message text.
type PostureEvent struct {
	Score      int    // 1..7+ final
	Risk       string // medium | high (negligible/low never reach here)
	LeftScore  int    // per-side final
	RightScore int

	// Per-side arm sub-scores (zero if not parseable from message). Used to
	// detect arm-asymmetry that the final side score flattens. Neck/trunk are
	// bilateral and identical L/R, so a small final-side delta with large arm
	// sub-score divergence still indicates a meaningful arm-side imbalance.
	LeftUpperArm  int
	RightUpperArm int
	LeftWrist     int
	RightWrist    int
}

const (
	// Praise eligibility window after a nudge.
	praiseMinAgeMin = 1
	praiseMaxAgeMin = 30
	// Repeated-in-episode window: a same-risk event within this many seconds
	// of the previous alert is "is_repeated".
	episodeWindowS = 600 // 10 min — looser than lelamp's 5min dedup
)

// postureContext is the digest the agent reads. Values are derived labels /
// booleans only — no raw scores or counts, those live in the message text.
// The `profile` + `progress` blocks turn the skill from a per-event reactor
// into a coach with a longitudinal view of the user.
type postureContext struct {
	Current         postureCurrent  `json:"current"`
	Session         postureSession  `json:"session"`
	Today           postureToday    `json:"today"`
	Profile         postureProfile  `json:"profile"`
	Progress        postureProgress `json:"progress"`
	PatternsNow     []string        `json:"patterns_now,omitempty"`
	BootstrapNeeded bool            `json:"bootstrap_needed"` // patterns.json missing/stale AND posture_days >= 3 → invoke habit Flow A only when nudging
}

type postureCurrent struct {
	Risk          string `json:"risk"`                     // medium | high
	Asymmetric    bool   `json:"asymmetric"`               // |L - R| >= 2
	DominantSide  string `json:"dominant_side"`            // left | right | both
	Trend         string `json:"trend"`                    // worsening | stable | improving | new
}

type postureSession struct {
	IsRepeated     bool `json:"is_repeated"`     // same risk seen earlier this episode
	PraiseEligible bool `json:"praise_eligible"` // recent nudge + improving
}

type postureToday struct {
	TimeOfDay string `json:"time_of_day"` // morning|noon|afternoon|evening|night
}

// postureProfile is the rolling user profile from the last week of posture
// alerts. Lets the agent talk about this user's *habits* — "lúc 15h hay
// bị", "tay phải hay nặng hơn" — instead of reacting to each event in
// isolation. Empty when the user has fewer than 5 alerts in the window.
type postureProfile struct {
	AlertsLast7d        int    `json:"alerts_last_7d"`              // total posture_alert rows in last 7 days
	PeakHourThisWeek    int    `json:"peak_hour_this_week"`         // 0-23, or -1 when not enough data
	SideBias            string `json:"side_bias"`                   // "left" | "right" | "none"  — which side scored worse more often
	TypicalRiskBucket   string `json:"typical_risk_bucket"`         // "medium" | "high" — most common bucket this week
}

// postureProgress frames whether the user is improving or regressing without
// quoting raw numbers. The agent uses these labels to choose tone (warm-up
// vs concern) and to weave a "compared to yesterday" line.
type postureProgress struct {
	TodayVsYesterday string `json:"today_vs_yesterday"` // worse | similar | better | unknown
	CurrentStreakMin int    `json:"current_streak_min"`  // minutes since the last alert (0 if this is the first)
}

// BuildPostureContext returns the JSON-encoded context block (without the
// `[posture_context: ]` wrapper) for injection into the agent prompt.
func BuildPostureContext(user string, ev PostureEvent) string {
	user = usercanon.Resolve(user)
	now := time.Now()
	today := now.Format("2006-01-02")
	hour := now.Hour()

	events := posture.Query(user, today, 0)

	ctx := postureContext{
		Current: postureCurrent{
			Risk:         strings.ToLower(ev.Risk),
			Asymmetric:   isAsymmetric(ev),
			DominantSide: dominantSide(ev.LeftScore, ev.RightScore),
			Trend:        computeTrend(events, ev.Score),
		},
		Session: postureSession{
			IsRepeated:     isRepeatedEpisode(events, ev.Score, now),
			PraiseEligible: praiseEligible(events, ev.Score, now),
		},
		Today: postureToday{
			TimeOfDay: timeOfDayBucket(hour),
		},
		Profile:  buildProfile(user, now),
		Progress: buildProgress(user, events, now),
		// PatternsNow stays nil until habit Flow A starts emitting
		// posture-pattern bands the agent can attach to the current hour.
		// Profile fields above already cover peak_hour / side_bias / typical_risk_bucket.
		PatternsNow:     nil,
		BootstrapNeeded: posturePatternsBootstrapNeeded(user),
	}

	buf, err := json.Marshal(ctx)
	if err != nil {
		slog.Warn("posture: context marshal failed", "error", err)
		return "{}"
	}
	return string(buf)
}

// isAsymmetric returns true when either (a) the final side scores diverge by
// 2+ points, or (b) per-side arm sub-scores diverge by 2+ points. Threshold
// 2 on the final score is conservative because neck/trunk (bilateral) drag
// the side delta toward zero; the sub-score check catches arm-driven
// imbalance that final-score totals flatten.
func isAsymmetric(ev PostureEvent) bool {
	if absInt(ev.LeftScore-ev.RightScore) >= 2 {
		return true
	}
	if absInt(ev.LeftUpperArm-ev.RightUpperArm) >= 2 {
		return true
	}
	if absInt(ev.LeftWrist-ev.RightWrist) >= 2 {
		return true
	}
	return false
}

func dominantSide(left, right int) string {
	switch {
	case left > right:
		return "left"
	case right > left:
		return "right"
	default:
		return "both"
	}
}

// computeTrend looks at the prior alert rows today and compares scores to
// the current. Returns "worsening", "stable", "improving", or "new" when
// no prior alert exists.
func computeTrend(events []posture.Event, currentScore int) string {
	for i := len(events) - 1; i >= 0; i-- {
		e := events[i]
		if e.Action != posture.ActionAlert || e.Score == 0 {
			continue
		}
		switch {
		case currentScore > e.Score:
			return "worsening"
		case currentScore < e.Score:
			return "improving"
		default:
			return "stable"
		}
	}
	return "new"
}

// isRepeatedEpisode reports whether an alert with the same risk-bucketed
// level fired within the recent episode window. Treats medium and high as
// distinct buckets so a high→medium drop is NOT "repeated".
func isRepeatedEpisode(events []posture.Event, currentScore int, now time.Time) bool {
	cutoff := float64(now.Unix() - episodeWindowS)
	currentRisk := riskBucket(currentScore)
	for i := len(events) - 1; i >= 0; i-- {
		e := events[i]
		if e.TS < cutoff {
			break
		}
		if e.Action == posture.ActionAlert && riskBucket(e.Score) == currentRisk {
			return true
		}
	}
	return false
}

// praiseEligible: a nudge was fired 1-30 min ago AND the current event has
// dropped to a lower risk bucket than the last alert. Praise gate per SKILL
// rule #1.
func praiseEligible(events []posture.Event, currentScore int, now time.Time) bool {
	nudgeTS := lastActionTS(events, posture.ActionNudge)
	if nudgeTS == 0 {
		return false
	}
	ageMin := int(now.Sub(time.Unix(int64(nudgeTS), 0)).Minutes())
	if ageMin < praiseMinAgeMin || ageMin > praiseMaxAgeMin {
		return false
	}
	// Trend must be improving — i.e. current bucket below most recent alert
	// before the praise window.
	for i := len(events) - 1; i >= 0; i-- {
		e := events[i]
		if e.Action == posture.ActionAlert && e.TS < nudgeTS {
			return riskBucket(currentScore) < riskBucket(e.Score)
		}
	}
	return false
}

func lastActionTS(events []posture.Event, action string) float64 {
	for i := len(events) - 1; i >= 0; i-- {
		if events[i].Action == action {
			return events[i].TS
		}
	}
	return 0
}

// riskBucket maps a RULA final score to its bucket index. Matches the
// `risk_name` vocabulary used by lelamp (1=negligible, 2=low, 3=medium,
// 4=high). Lumi only ever sees buckets 3 and 4 since lelamp filters lower.
func riskBucket(score int) int {
	switch {
	case score <= 2:
		return 1
	case score <= 4:
		return 2
	case score <= 6:
		return 3
	default:
		return 4
	}
}

func timeOfDayBucket(hour int) string {
	switch {
	case hour < 5:
		return "night"
	case hour < 11:
		return "morning"
	case hour < 14:
		return "noon"
	case hour < 18:
		return "afternoon"
	case hour < 22:
		return "evening"
	default:
		return "night"
	}
}

func absInt(x int) int {
	if x < 0 {
		return -x
	}
	return x
}

// buildProfile aggregates the last 7 daily posture files into a rolling
// user profile (peak hour, side bias, typical bucket). Reading 7 small
// JSONL files at every event is cheap — the daily file caps at ~200 rows
// and Query is sequential append.
func buildProfile(user string, now time.Time) postureProfile {
	weekly := posture.QueryLastDays(user, 7, 0)
	out := postureProfile{
		PeakHourThisWeek:  -1,
		SideBias:          "none",
		TypicalRiskBucket: "",
	}
	if len(weekly) == 0 {
		return out
	}
	var (
		hourCounts   [24]int
		leftWorse    int
		rightWorse   int
		bucketCounts = map[string]int{}
		alertCount   int
	)
	for _, e := range weekly {
		if e.Action != posture.ActionAlert {
			continue
		}
		alertCount++
		hourCounts[e.Hour]++
		switch {
		case e.LeftScore > e.RightScore:
			leftWorse++
		case e.RightScore > e.LeftScore:
			rightWorse++
		}
		bucketCounts[bucketLabel(e.Score)]++
	}
	out.AlertsLast7d = alertCount
	if alertCount >= 5 {
		// Peak hour
		peak, peakCount := 0, 0
		for h, c := range hourCounts {
			if c > peakCount {
				peak, peakCount = h, c
			}
		}
		if peakCount > 0 {
			out.PeakHourThisWeek = peak
		}
		// Side bias: only call it a bias when one side leads the other by
		// at least 50% of the count (avoids flagging 6 vs 5 as a pattern).
		switch {
		case leftWorse > rightWorse*3/2 && leftWorse > 0:
			out.SideBias = "left"
		case rightWorse > leftWorse*3/2 && rightWorse > 0:
			out.SideBias = "right"
		}
		// Typical bucket
		var topBucket string
		var topCount int
		for b, c := range bucketCounts {
			if c > topCount {
				topBucket, topCount = b, c
			}
		}
		out.TypicalRiskBucket = topBucket
	}
	return out
}

// buildProgress compares today's alert count to yesterday's and reports the
// minutes since the most recent alert today.
func buildProgress(user string, todayEvents []posture.Event, now time.Time) postureProgress {
	out := postureProgress{TodayVsYesterday: "unknown"}
	todayAlerts := 0
	var lastAlertTS float64
	for _, e := range todayEvents {
		if e.Action == posture.ActionAlert {
			todayAlerts++
			if e.TS > lastAlertTS {
				lastAlertTS = e.TS
			}
		}
	}
	yesterday := now.AddDate(0, 0, -1).Format("2006-01-02")
	ydAlerts := 0
	for _, e := range posture.Query(user, yesterday, 0) {
		if e.Action == posture.ActionAlert {
			ydAlerts++
		}
	}
	if ydAlerts > 0 || todayAlerts > 0 {
		switch {
		case todayAlerts > ydAlerts*5/4:
			out.TodayVsYesterday = "worse"
		case todayAlerts*5/4 < ydAlerts:
			out.TodayVsYesterday = "better"
		default:
			out.TodayVsYesterday = "similar"
		}
	}
	if lastAlertTS > 0 {
		out.CurrentStreakMin = int(now.Sub(time.Unix(int64(lastAlertTS), 0)).Minutes())
	}
	return out
}

func bucketLabel(score int) string {
	switch riskBucket(score) {
	case 3:
		return "medium"
	case 4:
		return "high"
	default:
		return ""
	}
}

// posturePatternsBootstrapNeeded returns true when habit/patterns.json is
// missing or stale AND the user has at least `bootstrapMinDays` of posture
// JSONL — i.e. enough data to be worth a habit Flow A rebuild. Mirrors
// the wellbeing bootstrap gate so posture skill can invoke Flow A only on
// nudge turns, not on every event.
func posturePatternsBootstrapNeeded(user string) bool {
	path := filepath.Join(usersDir, user, patternsSubpath)
	if info, err := os.Stat(path); err == nil {
		if time.Since(info.ModTime()) < patternsFreshAge {
			return false
		}
	}
	return countPostureDays(user) >= bootstrapMinDays
}

func countPostureDays(user string) int {
	dir := filepath.Join(usersDir, user, postureSubdir)
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0
	}
	n := 0
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".jsonl") {
			n++
		}
	}
	return n
}
