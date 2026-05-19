---
name: user-role
description: Neuroscience/BCI researcher building ESP32-based SSVEP visual stimulus systems; Chinese-native, deep domain expertise
metadata:
  type: user
---

User is doing **EEG / BCI / SSVEP research**, builds custom ESP32 firmware + Python control stacks for visual stimulus experiments. Native Chinese speaker, comfortable mixing Chinese and English in code (comments in 中文, identifiers in English).

Domain expertise is real and deep — uses terms like SSVEP, SSVEF, ERP, P300, CFF (critical fusion frequency), MEG, ECoG, phase-locking coherence without prompting. Asks about harmonic collisions, jitter sources, timing precision tiers. Treats sub-millisecond timing as a concrete engineering target, not theoretical.

**Parallel projects** (same Windows user tree, separate dirs under `C:\Users\thlab\Documents\`):
- `esp_prj/ss_client` — SSVEP stimulator (current focus)
- `esp_prj/eit-dac-sw`, `esp_prj/esp-pd`, `esp_prj/tiny-8chn-EEG-EIT` — other ESP-based EEG/EIT work
- `Claude-Projects/EEG-Viz-Att-experiment` — EEG visualization / attention paradigm

**Identity**: git user `zx`, email `paulxusilk@icloud.com`.

**How to apply**: skip SSVEP/ERP basics, jump straight to precision numbers and tradeoffs. For protocols, prefer ASCII text (he debugs via `idf.py monitor` + Wireshark/socket scripts). Hardware-side timestamping ("sensor打戳") is the recurring pattern across his projects — propose it by default for any event-timing question.
