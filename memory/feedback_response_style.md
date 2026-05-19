---
name: feedback-response-style
description: Wants concrete µs/ms numbers + tier-based comparisons; iterative requirements; make reasonable calls without re-asking
metadata:
  type: feedback
---

Prefers responses that give **concrete numbers** and **tier-based comparison tables**, not hand-waving.

**Why**: Multi-turn conversation 2026-05-19 — every response that landed well used tables like "Path A (200 µs jitter, ~50 lines, gptimer+IRAM), Path B (<100 ns, ~150 lines, RMT), Path C (~ppm, ~10 lines, predictive math)". Generic "it depends" answers got immediate "其精度能到多少us？" follow-ups demanding numbers.

**How to apply**:
- When discussing timing, hardware, or design tradeoffs, lead with a comparison table — columns like precision (µs/ns) / complexity (lines of code) / hardware requirement / when-to-use
- Back every number with a concrete reference (esp_timer LAC freq, GPIO ISR entry latency, WiFi CSMA RTT envelope, LEDC PWM cycle math)
- Frame options as tiers (A/B/C, simple/middle/expensive) so user can pick the cost/benefit point matching their experiment
- Don't ask "should I implement?" for clear next steps — make the reasonable call. User explicitly said: "make the reasonable call and continue; they'll redirect if needed"
- Chinese for prose, English for identifiers — that's how his code reads too
- Requirements arrive iteratively: he refines as he understands ("offset doesn't matter, focus on jitter"; "actually I also need a GUI"). Be ready to **reuse and extend** prior turn's design, not redesign from scratch.

**Avoid**:
- Qualitative "depends on your needs" answers without numerical bounds
- Asking permission for in-scope minor decisions
- Re-introducing concepts he already commands
- Multi-paragraph preamble before the actual answer
