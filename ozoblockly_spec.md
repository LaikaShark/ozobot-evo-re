# ozobot stack vm

the instruction set flashforth compiles down to. stack machine, postfix — operands get
pushed before the op, like forth. bytes `0x00–0x7F` push literals `0..127`, bytes
`0x80–0xFF` are instructions. the program gets wrapped in an envelope, framed, and flashed
as base-7 colors at 20hz. evo only

## execution model

- **literal** `n` (0..127) is one byte `n`. data bytes (branch offsets, addresses) are raw,
  range −128..255
- **negative literal** `−v` compiles to `push(v−1)` then `0x83` (bitwise not) — `~(v−1)` is
  `−v`. not `neg` (0x8b), which comes out off by one on hardware
- **variables** start at address `0x28` (40) and count up. read/write with `get` (0x92) /
  `set` (0x93), address on the stack. sensors and event state are just low-memory registers
  read the same way (see the registers table)
- **calls** — `call` (0x90) takes a 2-byte absolute address (hi, lo), functions end with `;`
  (0x91). a callee reads caller args with `pick` (0xa5), writes results back with `put`
  (0xa6), drops the caller frame with `pop` (0xa7)
- **program end** — `end` (0xae) stops the program / sets the robot mode
- **branches** are 3 bytes each. in the short form the 3rd byte is always `0x97`, an offset
  sentinel — not an instruction:

  | kind | short (\|off\| ≤ 127) | long |
  |------|------------------------|------|
  | conditional (if-false) | `80 off 97` | `b9 hi lo` |
  | unconditional jump | `ba off 97` | `8e hi lo` |
  | indirect | `b4 off 97` | *(too long → error)* |

## wire format

```
"CRYCYMCRW"  +  [ V  stackHi stackLo  lenHi lenLo ]  +  <program bytes>  +  CK  +  CMW
  frame head              header                            body        checksum  tail
```

- **frame head** `CRYCYMCRW`, base-7 values `304 320 302`
- **V** is the highest instruction bank the program touches (1 for core, up to 7 for evo
  extensions) — a firmware capability check
- **stack** is 16-bit, auto-sized to fill free ram: `total − globals − len − heap`, where
  `total` = 2048 (evo), `globals` = `max(vars, 56)`, `heap` = `2·(list/var count)`
- **len** is the 16-bit program byte count
- **CK** = `(256 − sum(all bytes)) & 0xFF` — the byte that makes the total ≡ 0 mod 256
- **tail** is `334` (colors `CMW`)
- **color encoding** — each value is 3 base-7 digits, msd first. digit → color:
  `0=K 1=R 2=G 3=Y 4=B 5=M 6=C`. `W` means "repeat previous color" and is never a data
  digit — the robot reads changes, so consecutive equal colors collapse to `W`

header examples: evo `move` → `01 07 c5 00 03`, evo `move`+`end` → `01 07 …`

## opcodes (0x80–0xD7)

`*(asm)*` = the assembler synthesizes these, you don't write them directly. `bank N` marks
an extension op that needs capability byte `V ≥ N` (see wire format) — everything else is
bank 1. an empty operand cell means the op takes/leaves nothing that matters to the stack

| byte | op | stack / operands | notes |
|------|----|------------------|-------|
| 0x80 | branch if-false, rel | `[80 off 97]` | *(asm)* |
| 0x81 | bitwise and | `a b — a&b` | |
| 0x82 | bitwise or | `a b — a\|b` | |
| 0x83 | bitwise not `~` | `a — ~a` | also negative-literal encoding |
| 0x84 | bitwise xor | `a b — a^b` | |
| 0x85 | + add | `a b — a+b` | |
| 0x86 | − subtract | `a b — a−b` | |
| 0x87 | * multiply | `a b — a*b` | |
| 0x88 | / divide | `a b — a/b` | |
| 0x89 | mod | `a b — a%b` | |
| 0x8a | not (logical) | `a — !a` | |
| 0x8b | neg (arithmetic) | `a — −a` | |
| 0x8c | rand | `m n — r` | |
| 0x8d | seed rng | `n —` | |
| 0x8e | jump absolute | `[8e hi lo]` | *(asm)* |
| 0x90 | call absolute | `[90 hi lo]` | *(asm)* |
| 0x91 | `;` return / end-of-fn | | |
| 0x92 | get var/sensor/mem | `addr — val` | |
| 0x93 | set var/mem | `val addr —` | |
| 0x94 | dup | `a — a a` | |
| 0x95 | for-loop step helper | | tentative |
| 0x96 | drop | `a —` | |
| 0x97 | short-branch tail sentinel | | not an instruction |
| 0x98 | turn / rotate | `angle speed —` | |
| 0x99 | execute color-code action | `code —` | |
| 0x9a | latch/poll event flags | | top of wait loops |
| 0x9b | wait (delay) | `centisec —` | |
| 0x9c | ≥ | `a b — a≥b` | |
| 0x9d | > | `a b — a>b` | |
| 0x9e | move | `dist speed —` | speed signed = direction |
| 0x9f | wheels set l/r | `left right —` | `0 0` = stop |
| 0xa0 | line-following enable | `bool —` | |
| 0xa1 | breakpoint | | debug |
| 0xa2 | and (logical) | `a b — a&&b` | |
| 0xa3 | or (logical) | `a b — a\|\|b` | |
| 0xa4 | = equals | `a b — a==b` | |
| 0xa5 | pick (read caller frame) | `depth — val` | |
| 0xa6 | put (write caller frame) | `val depth —` | |
| 0xa7 | pop (drop n from frame) | `n —` | |
| 0xa8 | abs | `a — \|a\|` | |
| 0xa9 | clamp helper (min) | | paired with 0xaa |
| 0xaa | clamp helper (max) | | |
| 0xac | yield (cooperative) | | |
| 0xad | clear event flags | | |
| 0xae | end program / set mode | `mode —` | |
| 0xaf | list indexof | | |
| 0xb0 | list append | | |
| 0xb1 | list get element | | |
| 0xb2 | list set element | | |
| 0xb3 | list remove | | |
| 0xb4 | branch indirect, rel | `[b4 off 97]` | *(asm)* |
| 0xb5 | list remove (variant) | | |
| 0xb7 | list length / bounds | | tentative |
| 0xb8 | led (main led) | `b g r —` | each 0–127 |
| 0xb9 | branch if-false, abs | `[b9 hi lo]` | *(asm)* |
| 0xba | jump relative | `[ba off 97]` | *(asm)* |
| 0xbc | shift left | | |
| 0xbd | shift right | | |
| 0xbe | list append (variant) | | |
| 0xbf | list pop / remove (variant) | | |
| 0xc5 | list element addressing | | |
| 0xc6 | reset line following | | |
| 0xc8 | play audio file — **deprecated** | `skinHi skinLo nameHi nameLo type —` | `ozobot.py` refuses to compile it; crashes/silent on hw |
| 0xc9 | evoleds (masked leds) | `mask r g b —` | |
| 0xca | beep / tone | `dur —` | |
| 0xcb | play note (midi) | `midi —` | |
| 0xcc | check/poll event | `eventId —` | |
| 0xce | read robot register | `regId — val` | bank 2 |
| 0xcf | ir-comm send / set message | | bank 2 |
| 0xd0 | wheels ×multiplier | | bank 4 |
| 0xd1 | stop audio | | bank 4 |
| 0xd2 | say / announce | `1 4 sub value 1 —` | bank 5; fixed 5-operand frame |
| 0xd3 | play note and wait (blocking) | `midi —` | bank 5 |
| 0xd4 | drive to next intersection | | bank 5 |
| 0xd5 | choose way at intersection | | bank 5 |
| 0xd6 | write file (persistent) | | bank 7 |
| 0xd7 | read file (persistent) | | bank 7 |

**⚠ 0xc7** is `kill`. nothing emits it and it's unverified, but do not flash it at hardware

## sensor / event registers

read with `get` (0x92) or `check-event` (0xcc), write with `set` (0x93)

| reg | meaning | | reg | meaning |
|-----|---------|--|-----|---------|
| 0x08 | lines-found count | | 0x14 | command-finished |
| 0x0d | command-recognized | | 0x1a | **button capture enable** (write 1) |
| 0x0e | surface colour (COLOR sensor) | | 0x24 | proximity-sensor base |
| 0x0f | line colour | | 0x28 | first user variable (40) |
| 0x10 | intersection type | | 0x2c–0x2f | message-received (4 dirs) |
| 0x11 | last-intersection-decision | | 0x30–0x33 | message-expired (4 dirs) |
| 0x12 | button-press count | | 0x3b | battery register (via 0xce) |
| 0x13 | real-time clock | | | |

**button capture** — a flashed program only sees button presses if it first writes `1` to
`0x1a` (`1 26 set` = `01 1a 93`). in flashforth: `TRUE BUTTON_CAPTURE set`

## constants

`FALSE=0 TRUE=1`. intersection directions: `STRAIGHT=1 LEFT=2 RIGHT=4 BACKWARDS=8
DEFAULT/RANDOM=7`
