# FlashForth robot verification campaign

38 flashable tests that validate the ground-truth work in `ozobot.py` on real hardware.
Built for **Ozobot Evo** (each program starts with `evo`) — the project's only target
hardware.

## How to run

```bash
python3 ozobot.py flash examples/tests/t00_led_red.ff       # fill terminal bg (WSL/headless); ENTER to flash
```

Maximize the window/terminal first so the robot has a large solid area, place the robot
face-down on it, and flash. Flash one test, record the result, move to the next.

### The LED oracle — how a test reports pass/fail

Most tests compute something and then **self-report with the LED**:

- 🟢 **GREEN = PASS** — the robot computed the expected result.
- 🔴 **RED = FAIL** — it computed the wrong result (a real bug).
- ⚫ **Nothing / no color** — the program didn't run at all (reception failed, or the
  robot rejected the program — e.g. a wrong capability byte). This is distinct from RED.

Oracle tests **hold the color in an infinite loop**, so it stays lit until you re-flash
or power-cycle — no need to catch a brief flash. Action tests (move/turn/audio) run once
and stop.

**Run Tier 1 first.** The oracle depends on `if/else`, `=`, and `led` being correct; Tier
1 proves those, so later GREEN/RED readings are trustworthy.

Danger check: no test emits opcode `0xC7` (the documented `kill`). Safe to flash.

---

## Tier 0 — Baseline (does flashing work at all?) · direct observation

| # | File | Validates |
|---|------|-----------|
| T00 | `t00_led_red.ff` | full flash→decode→run pipeline, `led`, literal push, `end` |

- **Expected:** robot LED turns **solid red**.
- **Pass:** LED is red.
- **Fail/error:** no reaction → the flash isn't being received (screen too small/dim,
  robot placement, refresh rate) — fix this before anything else. Wrong color → color
  encoding or `led` operand order (`b g r`) is wrong.

**T01 `t01_blink_rgb.ff`** — `wait` timing + repeated `led`.
- **Expected:** red ~1s → green ~1s → blue ~1s, then stop (blue latched).
- **Pass:** all three colors in order, ~1s each.
- **Fail:** wrong order → `led` channel order wrong. Wrong timing → `wait` unit wrong
  (should be centiseconds; `100 wait` = 1.0s).

**T02 `t02_move_fwd.ff`** — `move (dist speed)`.
- **Expected:** drives **forward** ~50 mm, then stops.
- **Pass:** forward motion, roughly straight.
- **Fail:** no motion, wrong direction, or curved → `move` operand order/units wrong.

**T03 `t03_turn.ff`** — `turn (angle speed)`.
- **Expected:** rotates in place ~**90°**.
- **Pass:** ~quarter turn.
- **Fail:** no/!=90° rotation, or it drives instead of turning → `turn` semantics wrong.

**T04 `t04_spin_wheels.ff`** — `wheels (left right)` + **negative operand** in a real op.
- **Expected:** spins in place ~1s (left wheel forward, right reverse), then stops.
- **Pass:** clean in-place spin.
- **Fail:** both wheels same way / no spin → `wheels` order or the `-30` literal is wrong
  (ties into T20).

---

## Tier 1 — Core language · LED oracle (run these first)

| # | File | Expression → expected | Validates |
|---|------|----------------------|-----------|
| T10 | `t10_add.ff` | `2 3 + 5 =` | `+` |
| T11 | `t11_arith_chain.ff` | `(2+3)*4 == 20` | `+`, `*`, ordering |
| T12 | `t12_mod.ff` | `20 mod 6 == 2` | `mod` |
| T13 | `t13_div.ff` | `17 / 5 == 3` | `/` (integer) |
| T14 | `t14_gt_true.ff` | `7 > 3` (true) | `>` true path |
| T15 | `t15_gt_false.ff` | `3 > 7` (false) | `>` false path + `else` |
| T16 | `t16_logic_and.ff` | `(1 and 0) == 0` | logical `and` |

- **Expected (all):** 🟢 GREEN.
- **Pass:** GREEN.
- **Fail:** 🔴 RED means that operator is wrong. ⚫ nothing means Tier 0 reception issue —
  don't trust any oracle result until T00 works.
- Note T15 is the one where the *false* branch must win; GREEN there confirms `else`
  routing and that a false comparison yields 0.

**T17 `t17_repeat3.ff`** — `repeat … loop` counted loop (+ the required `drop`).
- **Expected:** LED blinks **green exactly 3 times**, then stops.
- **Pass:** exactly 3 blinks.
- **Fail:** wrong count → loop counter math wrong. Never stops / garbage → the `drop`
  after `loop` (counter left on stack) is off, or branch resolution is broken.

**T18 `t18_while5.ff`** — `while dup 0 > do … 1 - loop` countdown.
- **Expected:** LED blinks **blue exactly 5 times**, then stops.
- **Pass:** exactly 5 blinks.
- **Fail:** wrong count / runaway → `while/do/loop` branch patching or `-`/`dup`/`>` wrong.

**T19 `t19_colon_call.ff`** — colon definition `:grn … ;` + `call`.
- **Expected:** 🟢 GREEN (held).
- **Pass:** GREEN.
- **Fail:** RED/nothing → `call`/`;` address handling or the def-skip jump prefix is wrong.

---

## Tier 2 — The critical fixes (highest-value tests)

**T20 `t20_neg_literal.ff` — KEY: negative-literal encoding.**
- Program: `-5 5 + 0 =` → oracle.
- **Expected:** 🟢 GREEN. `-5` now compiles to `04 83` (push 4, bitwise-NOT) = −5, so
  −5+5 = 0.
- **Pass:** GREEN → the `neg`→`~` fix is correct on hardware.
- **Fail:** 🔴 RED → `-5` is computing −4 (the old `neg`/0x8b bug) or `0x83` isn't
  bitwise-NOT on this firmware. **This is the single most important result** — it decides
  whether the negative-literal correction is right.

**T21 `t21_neg_abs.ff`** — `-100 abs 100 =`.
- **Expected:** 🟢 GREEN (abs of a negative literal).
- **Fail:** RED → negative literal or `abs` wrong.

**T22 `t22_neg_move.ff`** — negative distance in a real motion op.
- **Expected:** robot drives **backward** ~40 mm.
- **Pass:** reverse motion.
- **Fail:** forward/none → negative value handling wrong end-to-end.

**T23 `t23_vbank2_battery.ff` — KEY: capability byte V (bank 2).**
- `readReg` (0xce) is a bank-2 opcode, so the header must carry **V=2** (auto-computed).
- **Expected:** 🟢 GREEN (battery reads > 0).
- **Pass:** GREEN → V-computation works and the robot accepts V=2 + `readReg` returns data.
- **Fail:** ⚫ **nothing** is the telling failure here — it means the robot **rejected the
  program** because the capability byte was wrong (or `readReg` isn't 0xce). 🔴 RED would
  mean it ran but battery read 0 (odd — note it).

**T24 `t24_vbank5_audio.ff` — capability byte V (bank 5).**
- `beep-wait` (0xd3) is bank 5 → header **V=5**.
- **Expected:** a short tone, **then** 🟢 GREEN held.
- **Pass:** tone + GREEN → V=5 accepted.
- **Fail:** ⚫ nothing → rejected (V wrong). Tone but no green, or green but no tone → note
  which; isolates audio vs. capability.

---

## Tier 3 — Bitwise & shift

| # | File | Expression → expected | Notes |
|---|------|----------------------|-------|
| T30 | `t30_and_bitwise.ff` | `6 & 3 == 2` | AND `0x81` |
| T31 | `t31_or_bitwise.ff` | `4 \| 1 == 5` | OR `0x82` |
| T32 | `t32_xor_bitwise.ff` | `6 ^ 3 == 5` | XOR `0x84` |

- **Expected:** 🟢 GREEN. **Fail:** 🔴 RED → that bitwise opcode is wrong.

**T33 `t33_shl.ff` / T34 `t34_shr.ff` — HYPOTHESIS tests (characterization).**
- We believe `shl` (0xbc) / `shr` (0xbd) are **shift-by-one** primitives (×2 / ÷2).
  T33 checks `3 shl == 6`, T34 checks `12 shr == 6`.
- **If GREEN:** hypothesis confirmed (shift-by-one).
- **If RED:** the op does something else (e.g. shift-by-N, or needs the loop scaffolding
  the editor wraps around it). **Not necessarily a bug** — report RED so we can pin the
  real semantics. Please note if you can what value it *did* produce (hard without output;
  RED just tells us ≠ our guess).

---

## Tier 4 — Evo audio (needs to hear the speaker)

**T40 `t40_note.ff`** — `60 note 50 0 beep` (middle C, 0.5s, non-blocking).
- **Expected:** a ~0.5s tone. **Pass:** audible tone. **Fail:** silence → `note`/`beep`
  operand model wrong, or audio bank not accepted.

**T41 `t41_note_seq.ff`** — three blocking notes C-E-G.
- **Expected:** a rising three-note arpeggio, notes clearly separated (blocking).
- **Fail:** one blurred tone / silence → `beep-wait` (blocking, bank 5) not working.

**T42 `t42_raw_beep.ff` — EXPLORATORY (frequency encoding).**
- Raw `1 100 40 0 beep` (frequency bytes instead of a MIDI note).
- **Expected (guess):** *some* tone. **Report:** did it beep at all, and roughly how it
  compares in pitch to T40. Characterizes the `(fHi fLo)` frequency format.

**T43 — REMOVED.** `play-file` (0xc8) is the OzoBlockly-**deprecated** "play audio file"
block and is now a **compile error** in `ozobot.py` (see `DEPRECATED`). On Evo it either
crashes (single operand → stack underflow) or is silent (5-operand), and OzoBlockly marks
it do-not-use. Use `note`/`beep`/`beep-wait` (T40–T42) for Evo audio instead.

---

## Tier 5 — Evo sensors / registers

**T50 `t50_battery_threshold.ff`** — GREEN if battery > 20.
- **Expected:** 🟢 GREEN on a charged robot (🔴 RED only if nearly flat).

**T51 `t51_firmware.ff`** — firmware register reads nonzero.
- **Expected:** 🟢 GREEN → `readReg` returns real register data.
- **Fail:** RED (read 0) or nothing (rejected) → `readReg`/register id wrong.

**T52 `t52_surface_color.ff` — EXPLORATORY (needs a RED surface).**
- Reads surface color; GREEN if it reads RED. Place the robot on **red paper**.
- **Report:** GREEN on red / RED on other colors would confirm `EV_SURFACE checkEvent`.
  If always RED, the color read path or the color-code value differs — note it.

**T53 `t53_charger.ff` — EXPLORATORY.**
- Charger register nonzero → GREEN. **Report:** LED **on the charger** vs **off** it.
  Tells us whether `REG_CHARGER` + `readReg` reflect charger state.

---

## Tier 6 — Interactive

**T60 `t60_button.ff`** — loops; GREEN once the top button has been pressed.
- **Expected:** LED off/idle, then turns 🟢 GREEN after you **press the button**.
- **Pass:** press → green. **Fail:** never turns green → `EV_BUTTON`/`checkEvent` wrong.
- (Runs forever — power-cycle to end.)

---

## Tier 7 — IR communication (needs TWO Evo robots)

Flash **T70 to robot A** and **T71 to robot B**, then place them facing each other.

- **T70 `t70_ir_send.ff`** (A): enables the front emitter and broadcasts the value 55.
- **T71 `t71_ir_recv.ff`** (B): turns 🟢 GREEN when it receives 55.
- **Pass:** B goes green while A is broadcasting nearby.
- **Fail:** B never greens → `writeReg`/`readReg` IR register offsets or emitter enable
  wrong. Skip if you don't have a second robot.

---

## Tier 8 — Exploratory (uncertain semantics — feedback wanted)

These exercise opcodes whose exact conventions we could not fully pin from the compiler.
RED/no-op here is **information, not necessarily a bug**.

**T80 `t80_next_intersection.ff` — needs a black line/intersection.**
- Enables line following and calls `next-intersection`, then GREEN.
- **Report:** does it follow the line and stop/green at an intersection, or ignore it?
  `next-intersection` is a raw primitive that may need the editor's nav scaffolding.

**T81 `t81_file_roundtrip.ff` — persistent storage guess.**
- Writes 42 to "slot 0", reads it back, GREEN if equal. Operand order is a **guess**
  (`value slot`).
- **Report:** GREEN (round-trip works as guessed), RED (ran but mismatched → different
  operand order), or nothing (rejected). Any result pins the `write-file`/`read-file` API.

---

## Hardware

Target is the **Ozobot Evo only** — all tests are built in Evo mode and use Evo
capabilities. (This is the project's standing assumption; see CLAUDE.md.)

---

## Results log (fill in and send back)

| Test | Result (🟢/🔴/⚫/note) | Observation |
|------|----------------------|-------------|
| T00 | | |
| T01 | | |
| … | | |
| T20 | | ← negative-literal fix |
| T23 | | ← capability byte V |
| T33/T34 | | ← shift semantics |
| T42/T43 | | ← audio freq / file id |
| T80/T81 | | ← nav / files |

The ones I most want feedback on: **T20** (negative-literal fix), **T23/T24** (the V
capability byte — whether the robot accepts bank-2/5 programs), and the **exploratory**
tests (T33/T34, T42/T43, T52/T53, T80/T81) that pin down semantics I had to infer.
