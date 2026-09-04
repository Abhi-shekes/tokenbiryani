# Biryani Console — UI design

Companion to [PLAN.md](../PLAN.md). Covers the operator console (M5), the CLI surface,
and the visual system shared across both.

---

## 1. The three questions

An operator console is scanned, not read. Everything below is subordinate to the three
questions a user actually opens this thing to answer:

1. **Can I make a request right now — and if not, when?**
   The home screen must answer this in under two seconds, without interaction.
2. **Why is my bill what it is?**
   Which, for this product, means: *is the prompt cache working?* (PLAN §3)
3. **Which account served that request, and why that one?**
   Routing has to be inspectable or it can't be trusted.

Q1 sets the home screen. Q2 makes cache hit rate a top-level stat, not a detail-page
metric. Q3 justifies an entire screen that most gateways don't have.

Any component that doesn't serve one of these gets cut.

---

## 2. Time is the primary axis

Every quota in this system is a burn-down against a window that refills at a known
timestamp. That is unusual: most dashboards show *what happened*, but this one mostly
needs to show *what is about to be possible*.

So the console is built forward in time, not backward:

- Countdowns beat timestamps. `00:41` outranks `21:22:11Z`.
- The headline number is **capacity available now**, and next to it, **when the next
  tranche returns**.
- The signature component is a **capacity horizon** (§5) — projected pool headroom over
  the next 60 minutes, stepping up at each known reset. No general-purpose gateway
  dashboard can draw this, because none of them model per-credential reset times.

---

## 3. Visual system

### The brand-versus-state collision

The project's identity color is saffron. In an operations UI, amber already means
"attention", so the two compete. The first instinct was to let saffron *be* the attention
tier — brand and warning meaning coincide, since the subject of this product is capacity
running out.

**That was wrong, and the palette validator proved it.** Saffron against the critical red
scores ΔE 11.5 for normal vision and 5.1 under deuteranopia — two colors an operator
cannot reliably tell apart in a column of status pills, which is exactly where they sit.

The fix turned out to be semantic, not chromatic. **Cooling is not a warning.** An account
waiting on a rate-limit reset is the system working as designed: expected, self-healing,
nothing for anyone to do. Only a disabled credential — a rotated key, a `401` — is
actionable. Collapsing those two into one amber tier was a modeling error that the color
problem surfaced.

So the state ramp has no amber in it:

| State | Meaning | Operator action |
|---|---|---|
| **ready** (green) | serving traffic, headroom available | none |
| **cooling** (blue) | waiting on a reset; will self-heal | none — wait, or let the queue handle it |
| **disabled** (red) | auth failure, exhausted spend cap, breaker open | **yes** |

Which frees saffron entirely. It becomes brand and interactive — wordmark, links, focus
rings, selection — and never encodes a value.

> **Rules that follow.** Semantic color outranks brand color. Saffron never appears as a
> data mark. Status color never carries meaning alone; every pill has a text label.

### Palette

Cache gets its own hue (violet) because it is a *metric*, not a health state, and it is
the one number that quietly decides the bill.

| Token | Role | Light | Dark |
|---|---|---|---|
| `ready` | healthy, capacity available | `#12795B` | `#2FA47A` |
| `cooling` | waiting on a reset, queued, throttled | `#1F63C4` | `#4A85DE` |
| `critical` | disabled, auth failure, breaker open | `#A93125` | `#DA6355` |
| `cache` | cache hit rate, cached tokens, affinity | `#7A4FD0` | `#9068DE` |
| `brand` | wordmark, links, focus, selection — never a data mark | `#A6620F` | `#E9A94A` |
| `ground` / `panel` | page, surface | `#F4F6F9` / `#FFFFFF` | `#0B0E13` / `#131820` |
| `ink` / `ink-mid` / `ink-dim` | text ramp | `#111721` / `#46505F` / `#6E7887` | `#E8ECF2` / `#A7B2C0` / `#6B7686` |

Both four-color sets pass every check in the palette validator — lightness band, chroma
floor, CVD separation, normal-vision floor, and 3:1 contrast against their surface. Worst
adjacent pair is cooling↔ready at ΔE 19.9 normal / 18.3 deuteranopia, comfortably clear.

Dark mode is a **selected** palette, not an inversion — its own steps, validated against
the dark surface. Mark colors and text colors differ: marks use the validated band steps,
pill text uses a brighter tint on a washed background, because a 3:1 mark color is not a
4.5:1 text color.

Neutrals carry a slight blue bias so saffron reads as a deliberate warm accent.

**Dark-first.** Operators run this beside a terminal. Light is fully supported and gets
equal contrast care, but dark is the design's home.

### Type

The docs and the console split deliberately:

- **Docs read** — serif body (Source Serif 4), generous measure.
- **Console scans** — no serif. Archivo for UI and labels, JetBrains Mono for every
  number, identifier, duration and token count, always `tabular-nums`.

Digits must line up in columns. That single rule does more for a dense ops table than
any other typographic choice.

### Density

8px base grid, 4px sub-steps. Table rows 44px comfortable / 32px compact, toggleable —
tuned for pools of 4 to 20 accounts, which is the realistic range.

---

## 4. Surfaces

| Surface | Answers | Ships |
|---|---|---|
| **Pool** (home) | Q1, Q2 | M5 |
| **Requests** → **Request inspector** | Q3 | M5 |
| **Account detail** | limit windows over time, error breakdown by class, model mix, health event log | M5 |
| **Keys** | virtual keys, scopes, spend caps, per-key attribution | M6 |
| **Settings** | add/test credentials, pool assignment, priority, cost tier, routing weights | M5 |
| **CLI** `tokenbiryani status` | Q1, in the terminal where the user already is | M1 |

The CLI ships *first*, at M1, long before the web console. The audience is Claude Code
users who live in a terminal; a text pool summary is more valuable to them earlier than
a browser tab, and it forces the event model into shape before any UI depends on it.

---

## 5. Component inventory

**Headroom meter.** Three hairline bars stacked per account — requests, input tokens,
output tokens — because a pool can be rich in one and starved in another, and a single
merged bar hides exactly the case that matters. Always paired with a mono label giving
the real numbers; the bar is for scanning, the label is for trusting.

**State pill.** Text plus color, never color alone. `ready` / `cooling` / `disabled`.
Cooling carries its countdown inside the pill, because "when" is the only thing anyone
wants from a cooling account.

**Reset countdown.** `mm:ss`, tabular, counting down live. Ticks once per second; nothing
else on the page animates.

**Capacity horizon.** 60-minute projection of aggregate available input-token capacity,
in 5-minute buckets, stepping up at each known reset timestamp. A "now" marker at the
left edge. This is the component that answers "when can I work again" without arithmetic.

**Attempt chain.** A request's failover path as a horizontal sequence:
`acct-02 → 429 (41ms) → acct-01 → 200 (4.16s)`. Makes failover legible at a glance;
a single-attempt request renders as one segment and costs nothing.

**Score breakdown.** Per-candidate table for one routing decision — the weighted terms
from PLAN §5, the resulting score, and the verdict (`chosen` / `filtered: no headroom` /
`filtered: model unsupported`). This is the trust-building surface of the whole product.

**Cache badge.** Violet. Shows hit rate and, on a request, `cache_read` vs
`cache_creation` token split. A **cache break** renders as a distinct marker on the
request stream — it is the one event that silently costs money, so it gets visual weight
out of proportion to its frequency.

---

## 6. States that must be designed, not defaulted

- **First run — no accounts.** This is the OSS activation funnel and deserves the most
  design attention of any screen: add credential → test it → copy the three `export`
  lines. Three steps, one screen, no navigation.
- **Whole pool cooling.** The console's worst moment must still be useful: show the
  horizon, the earliest reset, and the queue — not an empty table.
- **Single account.** Many users will start here. The pool table must not look broken
  with one row; degrade to a single detailed card.
- **Degraded credential.** A `401` disables an account. That needs to be loud and
  actionable — it means someone rotated a key.

---

## 7. Accessibility

- State is never carried by color alone: every pill has a label, every bar has a numeric
  readout. The palette is validated for deuteranopia, protanopia and tritanopia rather
  than eyeballed — see the brand-versus-state note above for what that caught.
- Contrast targets: 4.5:1 body, 3:1 for bars, meters and chart marks, checked in both
  themes.
- Countdowns are the only motion. Everything else respects `prefers-reduced-motion`; the
  live stream appends without transitions when reduced motion is set.
- Full keyboard path through the pool table into a request inspector; visible focus in
  saffron, which is why the brand hue is kept off the state ramp.

---

## 8. Build

Server-rendered HTML plus SSE for live updates. No SPA framework, no build step — the
console ships inside the Python package and must not add a Node toolchain to a
`pipx install`. Charts are hand-drawn SVG against theme tokens; the horizon and the
sparklines are the only two chart forms, and neither justifies a library.
