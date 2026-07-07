# ozobot flash code

reverse-engineering the ozobot evo's colorblast protocol. turns out the whole protocol is a little
stack vm and a base-7 color encoding, so we wrote a forth-y language (flashforth) that
compiles down to it and a flasher to program the bot

everything is in `ozobot.py`

## quickstart

```bash
python3 ozobot.py selftest                    # tests the self
python3 ozobot.py compile examples/blink.ff   # compiles and outputs the bytes and colorblast
python3 ozobot.py flash  examples/blink.ff    # actually runs the colorblast
python3 ozobot.py disasm "WCRYCYM..."         # colorblast string to instructions
```

`compile`/`flash` take a flashforth (`.ff`) file, `-` for stdin.

## a flashforth program

```forth
\ blink the led red, green, blue with one-second pauses
127 0 0 led  100 wait
0 127 0 led  100 wait
0 0 127 led  100 wait
OFF end
```

it's a stack language, so operands come before the word. `127 0 0 led` pushes r/g/b then
calls `led`. evo programs start with the `evo` word (sets the evo header + capability
bytes)

### quirks we kept on purpose

these look like bugs but they match what the real compiler does, so don't "fix" them (unless you want to, you do you. just watch out!)

- `repeat … loop` leaves a counter on the stack, gotta manually `drop` it (see
  `examples/pulse.ff`)
- colon-def names are `:red`, not `: red` like regular forth
- `evo` is a word in the source, not a flag

## how the protocol works

- **colors**: 8 of them, k r g b c m y w. three colors encode one byte in base-7. white
  means "repeat last color" — the robot reads transition so the
  encoder never shows the same color twice in a row and uses white as an explicit repeat
- **envelope**: `VER_HI VER_LO LEN_MAGIC 00 LEN … CHECKSUM`, checksum is running byte
  subtraction from zero. evo is version `01 07`, magic `199`, programs prepended with
  `2d 28 93` and terminated `03 end`
- **vm**: values 0–127 push a literal, `0x80+` are instructions (~88 of them, `0x80–0xD7`).
  variables start at `0x25`, sensors are just variable reads. full opcode map is in
  `OZOBLOCKLY_VM.md`

### DON'T FLASH `0xC7`

opcode `0xC7` is killer. it bricks starter-pack ozobots. we're not
emitting that anywhere near a real robot and neither should you

## whats in here

- `ozobot.py` the whole toolchain. compiler, encoder, flasher, disassembler, selftest
- `OZOBLOCKLY_VM.md` ~88-opcode instruction set
- `examples/` — sample programs
- `flash_recorder.py` samples the screen pixel under your mouse
  and logs the flash sequence back into something `disasm` can eat. handy tool for RE

## testing on a real robot

`examples/tests/` has a whole bunch of things, green is good, red is bad, there's a text file we printed off to checklist and write notes if you want it for some reason.

we only tested and targeted the evo, but other things are in there for completion of the colorblast protocol

have fun! <3
