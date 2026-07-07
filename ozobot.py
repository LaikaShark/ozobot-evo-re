#!/usr/bin/env python3
"""Standalone FlashForth compiler / encoder / flasher for Ozobot robots.

A pure-stdlib port of the (now-removed) browser IDE's compiler/encoder plus the
F# writer/disassembler tools. No web browser and no .NET/Mono required.

FlashForth is a tiny Forth-like language that compiles to Ozobot VM bytecode. The
bytecode is wrapped in a version/length/checksum envelope, framed, and encoded as
a base-7 sequence of screen colors flashed at 20Hz to the robot's color sensor.

Subcommands:
    compile PROG.ff     compile to bytecode + color string
    flash   PROG.ff     compile and flash by painting the terminal background
    disasm  COLORS       decode a color string back to bytecode
    selftest             run golden-vector + round-trip checks

Ported byte-for-byte from the original compiler, quirks and all; ground-truth
corrections are documented in OZOBLOCKLY_VM.md (see NOTES).
"""

import argparse
import sys

# --------------------------------------------------------------------------- #
# 1. Protocol tables
# --------------------------------------------------------------------------- #

# base-7 digit -> color letter (index == digit). White (7) is reserved as the
# "repeat" separator and is never a data digit. BGR 3-bit: digit = R + 2G + 4B.
DIGIT_TO_COLOR = "KRGYBMC"
COLOR_TO_DIGIT = {c: i for i, c in enumerate(DIGIT_TO_COLOR)}  # W excluded

# Framing sync markers. Each is emitted as three base-7 digits like every other
# value, but decodes outside single-byte range so the robot can find it. The head
# is the literal color prefix "CRYCYMCRW" the editor prepends (values 304 320 302);
# the tail is 334 ("CMW"). See OZOBLOCKLY_VM.md "Wire format".
FRAME_HEAD = [304, 320, 302]   # "CRYCYMCRW"
FRAME_TAIL = 334               # "CMW"

# Words that map to a single opcode byte. The first block is the original
# index.html:149-214 set; the second are opcodes recovered from the live
# OzoBlockly compiler (see OZOBLOCKLY_VM.md "Opcodes" for the full 0x80-0xD7 map).
OPCODES = {
    '~': 0x83, '+': 0x85, '-': 0x86, '*': 0x87, '/': 0x88, 'mod': 0x89,
    'not': 0x8a, 'neg': 0x8b, 'rand': 0x8c, 'dup': 0x94, 'drop': 0x96,
    'turn': 0x98, 'wait': 0x9b, '>=': 0x9c, '>': 0x9d, 'move': 0x9e,
    'wheels': 0x9f, 'and': 0xa2, 'or': 0xa3, '=': 0xa4, 'pick': 0xa5,
    'put': 0xa6, 'pop': 0xa7, 'abs': 0xa8, 'end': 0xae, 'led': 0xb8,
    'evoLeds': 0xc9, ';': 0x91, 'sensor': 0x92, 'get': 0x92, 'set': 0x93,
    # --- ground-truth additions (single-byte, unambiguous stack ops) ---
    '&': 0x81, '|': 0x82, '^': 0x84,           # bitwise AND / OR / XOR (a b -- c)
    'seed': 0x8d,                              # seed the RNG (n --)
    'lineFollow': 0xa0,                        # enable/disable line following (bool --)
    'yield': 0xac,                             # cooperative yield (--)
    'clearEvents': 0xad,                       # clear pending event flags (--)
    'resetLine': 0xc6,                         # reset line-following state (--)
    # --- Evo sensor / register access ---
    'checkEvent': 0xcc,      # read an event register    (EV_* -- value)
    'readReg': 0xce,         # read a state register     (REG_* -- value)
    'writeReg': 0xcf,        # write a state register    (value REG_* --)
    # --- Evo audio (see the note/beep operand model in NOTES) ---
    'note': 0xcb,            # MIDI note -> frequency pair (midi -- fHi fLo)
    'beep': 0xca,            # play tone, non-blocking     (fHi fLo dur flag --)
    'beep-wait': 0xd3,       # play tone, blocking         (fHi fLo dur flag --)
    'stop-audio': 0xd1,      # stop playback               (type flag --)
    'play-file': 0xc8,       # DEPRECATED "play audio file" -- blocked by DEPRECATED; disasm only
    'say': 0xd2,             # announce/expressive audio   (see NOTES; drop result)
    # --- misc single-byte ops ---
    'execCode': 0x99,            # run a built-in color-code behavior (code --)
    'breakpoint': 0xa1,          # debugger breakpoint (--)
    'wheels-mult': 0xd0,         # set wheel speeds with a multiplier
    # --- Evo line navigation (raw primitives; used inside compiled routines) ---
    'next-intersection': 0xd4,   # advance to next intersection
    'choose-way': 0xd5,          # pick a way at an intersection
    # --- Evo persistent storage ---
    'write-file': 0xd6, 'read-file': 0xd7,
    # --- list/array primitives (raw; require the list-memory scaffolding the
    #     editor generates -- see NOTES. Exposed for manual use / disassembly). ---
    'list-indexOf': 0xaf, 'list-append': 0xb0, 'list-get': 0xb1, 'list-set': 0xb2,
    'list-remove': 0xb3, 'list-remove-i': 0xb5, 'list-len': 0xb7,
    'list-grow': 0xbe, 'list-pop': 0xbf, 'list-addr': 0xc5,
    # --- variable-count bit-shift primitives (shift by one; loops build N-shifts) ---
    'shl': 0xbc, 'shr': 0xbd,
}

# Opcode -> instruction-bank level. Using any of these requires the header's
# capability byte V >= the given bank (else the robot may reject the program).
# Bytes not listed are bank 1. Verified against the live compiler (0xce->2,
# 0xd6->7). See OZOBLOCKLY_VM.md "Wire format".
# NB: non-blocking audio 0xc8/0xca/0xcb are bank 1 -- the Evo runs them at V=1 but
# *shuts down* if the header claims V=5 (tested 2026-07-06). Do not bank them up.
OPCODE_BANK = {0xce: 2, 0xcf: 2, 0xd0: 4, 0xd1: 4,
               0xd2: 5, 0xd3: 5, 0xd4: 5, 0xd5: 5, 0xd6: 7, 0xd7: 7}

# NOTES -- operand models for the Evo words above (from the OzoBlockly generators).
#
# Audio.  `note` (0xcb) converts a MIDI number to a 2-byte frequency, `beep`
#   (0xca) plays it: signature (fHi fLo dur flag --), dur in centiseconds, flag 0.
#   So a middle-C for 500ms is:      60 note  50 0 beep
#   Use `beep-wait` (0xd3) for the blocking variant. Raw tone (no note): push the
#   frequency bytes yourself, e.g. `fHi fLo 50 0 beep`.
#
# say (0xd2).  Expressive/number-to-speech primitive with a fixed operand frame;
#   the editor wraps it as:          1 4 <sub> <value> 1 say drop
#   where <sub> is 2 = say-colour, 1 = say-direction. (say-number is a larger
#   compiled routine, not a single op.)
#
# Registers.  `readReg` (regId -- v) reads a state register; `writeReg`
#   (v regId --) writes one. IR comm is register access at a per-sensor offset:
#     send a message :   <value> REG_IR_MSG <proxId> +  writeReg
#     read a message :   REG_IR_RECV <proxId> +  readReg
#     enable emitter :   127 REG_IR_EMITTER <proxId> +  writeReg
#   Simple sensors: `REG_BATTERY readReg`, `REG_SURFACE_PROX readReg`, etc.
#   Proximity distance is a *variable* read, not a register: <proxId> REG_PROX + get.
#
# checkEvent (eventId -- v).  e.g. `EV_BUTTON checkEvent`, `EV_INTERSECTION checkEvent`.
#
# Lists/arrays.  The `list-*` opcodes are raw primitives. Functional lists also
#   need the editor's list-memory scaffolding (a length system-variable, a computed
#   storage offset, temp vars) which FlashForth does NOT generate -- so these are
#   exposed for manual use and disassembly only, not as turn-key list ops.
#
# Capability byte V is computed automatically from the opcodes used (OPCODE_BANK).

# Words that push a single constant byte (same code path as an integer literal).
CONSTANTS = {
    'TRUE': 1, 'FALSE': 0,
    'OFF': 0, 'FOLLOW': 1, 'IDLE': 2,          # `end` modes
    'COLOR': 14, 'LINE': 10,                    # sensor selectors
    'BLACK': 0, 'RED': 1, 'GREEN': 2, 'YELLOW': 3,
    'BLUE': 4, 'MAGENTA': 5, 'CYAN': 6, 'WHITE': 7,
    'STRAIGHT': 1, 'LEFT': 2, 'RIGHT': 4, 'END': 8, 'BACK': 8,
    # --- event register ids (ground truth): operand for `checkEvent` (0xcc), or
    # read as a variable with `get`/0x92. See OZOBLOCKLY_VM.md "Opcodes". ---
    'EV_NUM_LINES': 0x08, 'EV_CMD_RECOGNIZED': 0x0d, 'EV_SURFACE': 0x0e,
    'EV_LINECOLOR': 0x0f, 'EV_INTERSECTION': 0x10, 'EV_LAST_DECISION': 0x11,
    'EV_BUTTON': 0x12, 'EV_CLOCK': 0x13, 'EV_CMD_FINISHED': 0x14,
    'EV_MSG_RECV': 0x2c,      # +0..3 = left-rear/left-front/right-rear/right-front
    'EV_MSG_EXPIRED': 0x30,   # +0..3, same sensor order
    # Button capture: presses reach the program ONLY after you write 1 here with
    # `set` (0x93), else they hit the default system handler. Ground truth: the
    # OzoBlockly "capture button press = true" block compiles to `1 26 set`
    # (captured + disassembled from a real flash). Use: `TRUE BUTTON_CAPTURE set`.
    'BUTTON_CAPTURE': 0x1a,
    # --- state register ids: operand for `readReg` (0xce) / `writeReg` (0xcf) ---
    'REG_PROX': 0x24,         # proximity base; read via get: <base+id> 0x24 + get
    'REG_IR_MSG': 0x28,       # +proxId, writeReg: value (base+id) writeReg
    'REG_IR_EMITTER': 0x34,   # +proxId, writeReg: enable(0/127) (base+id) writeReg
    'REG_IR_RECV': 0x2c,      # +proxId, readReg
    'REG_IR_STRENGTH': 0x30,  # +proxId, readReg
    'REG_SURFACE_PROX': 0x38, 'REG_ROBOT_COLOR': 0x3a, 'REG_BATTERY': 0x3b,
    'REG_SMARTSKIN_A': 0x3c, 'REG_SMARTSKIN_B': 0x3d, 'REG_CHARGER': 0x3e,
    'REG_BLUETOOTH': 0x3f, 'REG_FIRMWARE': 0x42,
}

# Hidden opcodes used by the compiler itself (not user words).
OP_CALL, OP_IF, OP_JUMP, OP_PAD, OP_RET = 0x90, 0x80, 0xba, 0x97, 0x91

# Complete ground-truth opcode -> mnemonic map (0x80-0xD7), extracted from the live
# OzoBlockly compiler. Used to annotate `disasm` output. Names marked "(asm)" are
# synthesized by the assembler, never emitted as a user word; "?" = present in the
# instruction enum but unused by the editor. See OZOBLOCKLY_VM.md "Opcodes".
VM_OPCODES = {
    0x80: 'branch(rel)', 0x81: '&', 0x82: '|', 0x83: '~', 0x84: '^',
    0x85: '+', 0x86: '-', 0x87: '*', 0x88: '/', 0x89: 'mod', 0x8a: 'not',
    0x8b: 'neg', 0x8c: 'rand', 0x8d: 'seed', 0x8e: 'jump(abs)', 0x8f: '?',
    0x90: 'call', 0x91: ';', 0x92: 'get', 0x93: 'set', 0x94: 'dup',
    0x95: 'for-step', 0x96: 'drop', 0x97: 'branch-pad', 0x98: 'turn',
    0x99: 'exec-code', 0x9a: 'event-poll', 0x9b: 'wait', 0x9c: '>=', 0x9d: '>',
    0x9e: 'move', 0x9f: 'wheels', 0xa0: 'lineFollow', 0xa1: 'breakpoint',
    0xa2: 'and', 0xa3: 'or', 0xa4: '=', 0xa5: 'pick', 0xa6: 'put', 0xa7: 'pop',
    0xa8: 'abs', 0xa9: 'clamp-min', 0xaa: 'clamp-max', 0xab: '?', 0xac: 'yield',
    0xad: 'clearEvents', 0xae: 'end', 0xaf: 'list-indexOf', 0xb0: 'list-append',
    0xb1: 'list-get', 0xb2: 'list-set', 0xb3: 'list-remove', 0xb4: 'branch-i',
    0xb5: 'list-remove', 0xb6: '?', 0xb7: 'list-len', 0xb8: 'led',
    0xb9: 'branch(abs)', 0xba: 'jump', 0xbb: '?', 0xbc: 'shl', 0xbd: 'shr',
    0xbe: 'list-append', 0xbf: 'list-pop', 0xc0: '?', 0xc1: '?', 0xc2: '?',
    0xc3: '?', 0xc4: '?', 0xc5: 'list-addr', 0xc6: 'resetLine', 0xc7: '?(kill)',
    0xc8: 'play-file', 0xc9: 'evoLeds', 0xca: 'beep', 0xcb: 'note',
    0xcc: 'check-event', 0xcd: '?', 0xce: 'reg-read', 0xcf: 'ircomm',
    0xd0: 'wheels-mult', 0xd1: 'stop-audio', 0xd2: 'say', 0xd3: 'note-wait',
    0xd4: 'next-intersection', 0xd5: 'choose-way', 0xd6: 'write-file',
    0xd7: 'read-file',
}

# Reverse map for disassembly annotation: start from the full ground-truth set,
# then prefer FlashForth's canonical word where one exists.
_OPCODE_NAME = dict(VM_OPCODES)
_OPCODE_NAME.update({OP_CALL: 'call', OP_IF: 'if', OP_JUMP: 'jump', OP_RET: ';'})


class AssembleError(Exception):
    """Raised on a source construct the compiler rejects."""


# --------------------------------------------------------------------------- #
# 2. assemble(source) -> (bytecode, evo_mode)
# --------------------------------------------------------------------------- #

class _Ctx:
    """Mutable compile state, replacing the JS module globals."""

    def __init__(self):
        self.asm = []              # emitted bytes
        self.stack = []            # compiler_stack: branch addrs / loop kinds
        self.user = {}             # user-defined colon words -> emit closure
        self.evo = False

    def push(self, b):
        self.asm.append(b & 0xff)

    def here(self):
        return len(self.asm)


# ---- macros (index.html:42-147) ------------------------------------------- #
# Forward branch placeholders hold an absolute asm index; the patched value is
# the relative forward distance (target - addr + 1). Backward jumps store the
# two's-complement byte (-back) & 0xff.

def _m_if(ctx):
    ctx.push(OP_IF)
    ctx.stack.append(ctx.here())   # remember placeholder index
    ctx.push(0x00)                 # placeholder distance
    ctx.push(OP_PAD)


def _m_else(ctx):
    ifaddr = ctx.stack.pop()
    ctx.push(OP_JUMP)
    ctx.stack.append(ctx.here())
    ctx.push(0x00)
    ctx.push(OP_PAD)
    ctx.asm[ifaddr] = ctx.here() - ifaddr + 1   # if -> past the jump


def _m_then(ctx):
    addr = ctx.stack.pop()
    ctx.asm[addr] = ctx.here() - addr + 1


def _m_while(ctx):
    ctx.stack.append(ctx.here())   # mark predicate start


def _backjump(ctx):
    addr = ctx.stack.pop()
    ctx.push(OP_JUMP)
    back = ctx.here() - addr - 1
    ctx.push((-back) & 0xff)
    ctx.push(OP_PAD)


def _m_endwhile(ctx):
    ifaddr = ctx.stack.pop()       # the `do`/if placeholder
    _backjump(ctx)                 # pops the predicate-start addr
    ctx.asm[ifaddr] = ctx.here() - ifaddr + 1


def _m_forever(ctx):
    ctx.stack.append(ctx.here())   # loop top
    ctx.stack.append(1)            # kind = forever


def _m_do(ctx):
    _m_if(ctx)
    ctx.stack.append(0)            # kind = while


def _m_loop(ctx):
    kind = ctx.stack.pop()
    if kind == 0:
        _m_endwhile(ctx)
    elif kind == 1:
        _backjump(ctx)             # end-forever: backward jump only
    else:
        raise AssembleError('Unknown loop kind: %r' % kind)


def _m_repeat(ctx):
    # N repeat ... loop -> counted loop. Counter is left on the stack on exit
    # (the JS drop lives in dead code; we intentionally do not emit it).
    _m_while(ctx)
    ctx.push(1)
    ctx.push(OPCODES['-'])
    ctx.push(OPCODES['dup'])
    ctx.push(0)
    ctx.push(OPCODES['>='])
    _m_do(ctx)


def _m_lt(ctx):   # <  ==  >= not
    ctx.push(OPCODES['>='])
    ctx.push(OPCODES['not'])


def _m_leq(ctx):  # <= ==  > not
    ctx.push(OPCODES['>'])
    ctx.push(OPCODES['not'])


def _m_neq(ctx):  # <> ==  = not
    ctx.push(OPCODES['='])
    ctx.push(OPCODES['not'])


def _m_evo(ctx):
    ctx.evo = True
    ctx.push(0x2d)
    ctx.push(0x28)
    ctx.push(0x93)


MACROS = {
    'if': _m_if, 'else': _m_else, 'then': _m_then,
    'while': _m_while, 'do': _m_do, 'loop': _m_loop,
    'forever': _m_forever, 'repeat': _m_repeat,
    '<': _m_lt, '<=': _m_leq, '<>': _m_neq, 'evo': _m_evo,
}

# Words the assembler refuses to compile -> {name: reason}. `play-file` (0xc8) is
# deprecated in OzoBlockly ("play audio file" is marked deprecated / do-not-use) and its
# operand model was never pinned; on Evo it crashes (single operand underflows) or is
# silent (5-operand). Blocked to keep it off real robots. Still decodable in disasm.
DEPRECATED = {
    'play-file': 'OzoBlockly deprecates the "play audio file" block (0xc8); '
                 'do not use it. Use note/beep/beep-wait for Evo audio.',
}


def assemble(source):
    """Compile FlashForth source into (bytecode list, evo_mode bool)."""
    ctx = _Ctx()
    lines = source.splitlines()
    tokens_per_line = [ln.split() for ln in lines]

    # Pass 1: if the program defines colon words, prefix a jump that skips over
    # the inlined definition bodies to the start of the main program. Stop at `\`
    # so a ':' or ';' inside a comment does not trigger a spurious prefix.
    def _defines(line):
        for w in line:
            if w.startswith('\\'):
                break
            if w.startswith(':') or w == ';':
                return True
        return False
    has_defs = any(_defines(line) for line in tokens_per_line)
    if has_defs:
        ctx.push(OP_JUMP)
        ctx.push(0x00)   # asm[1] placeholder, patched by each `;`
        ctx.user = {}

    # Pass 2: emit bytecode.
    for words in tokens_per_line:
        i = 0
        while i < len(words):
            word = words[i]
            i += 1

            if word.startswith('\\'):
                break  # line comment to end of line

            if word.startswith(':') and len(word) > 1:
                _define(ctx, word[1:])
                continue

            if word == ';':
                ctx.asm[1] = ctx.here() + 1   # patch leading jump past last def
                # fall through: `;` is also an opcode word (0x91)

            lit = _parse_int(word)
            if lit is not None:
                _emit_literal(ctx, lit)
                continue

            if word in DEPRECATED:
                raise AssembleError('%s is deprecated: %s' % (word, DEPRECATED[word]))

            if word in ctx.user:
                ctx.user[word](ctx)
            elif word in MACROS:
                MACROS[word](ctx)
            elif word in OPCODES:
                ctx.push(OPCODES[word])
            elif word in CONSTANTS:
                ctx.push(CONSTANTS[word])
            else:
                raise AssembleError('Unknown word: %r' % word)

    return ctx.asm, ctx.evo


def _define(ctx, name):
    """Register a colon word whose body is compiled inline at the current addr."""
    calladdr = ctx.here()

    def emit_call(c):
        c.push(OP_CALL)
        c.push(calladdr >> 8)
        c.push(calladdr & 0xff)

    ctx.user[name] = emit_call


def _parse_int(word):
    """Return the integer value of a decimal literal, or None if not a literal.

    Matches JS parseInt: decimal only (no x7F hex). Rejects out-of-range here.
    """
    try:
        v = int(word, 10)
    except ValueError:
        return None
    if v < -128 or v > 127:
        raise AssembleError('Literal out of range (-128..127): %s' % word)
    return v


def _emit_literal(ctx, i):
    if i >= 0:
        ctx.push(i)                # 0x00..0x7f is push-immediate
    else:
        # Negative literal: push |i|-1, then bitwise NOT (~(|i|-1) == i).
        # OzoBlockly emits ~ (0x83) here. index.html/FlashAsm used neg (0x8b),
        # which computes -(|i|-1) on the robot -- off by one (-127 -> -126).
        # Corrected to the ground-truth encoding; see OZOBLOCKLY_VM.md "Execution model".
        ctx.push(~i & 0xff)        # == |i| - 1
        ctx.push(OPCODES['~'])     # 0x83  (was neg/0x8b)


# --------------------------------------------------------------------------- #
# 3. encode(code, evo) -> color string
# --------------------------------------------------------------------------- #

def _checksum(pre):
    chk = 0
    for b in pre:
        chk = (chk - b) & 0xff
    return chk


def envelope(code, evo=False):
    """Wrap bytecode with the 5-byte header + checksum (no framing).

    Ground truth (OZOBLOCKLY_VM.md "Wire format"): the header is [V, stackHi, stackLo,
    lenHi, lenLo], where V = instruction-bank level (1 for core programs) and
    stack auto-fills free RAM. The historical reading below -- version [1,3]/[1,7]
    plus a "219/199 - n" length magic -- is really [V=1, stackHi, stackLo=...]:
    stackHi is 3 for Bit (~981-byte stack) / 7 for Evo (~1989), and the "magic"
    is stackLo. The exact stackLo the editor emits is ~216-n (Bit); the 219/199-n
    kept here matches FlashAsm's golden vectors and is accepted by real robots, so
    it is intentionally left unchanged.
    """
    stack_hi = 7 if evo else 3               # ~1989 (Evo) / ~981 (Bit) stack, hi byte
    v = max([1] + [OPCODE_BANK.get(b, 1) for b in code])   # capability = max bank used
    ver = [v, stack_hi]                      # [V, stackHi]
    n = len(code)
    length = [((199 if evo else 219) - n) & 0xff, 0, n]   # [stackLo, lenHi, lenLo]
    pre = ver + length + list(code)
    return pre + [_checksum(pre)]


def encode(code, evo=False):
    """Compile output -> flash color string (with the leading White, as JS blink)."""
    framed = FRAME_HEAD + envelope(code, evo) + [FRAME_TAIL]
    enc = ['W']   # stream starts on White (neutral separator)
    for v in framed:
        for digit in ((v // 49) % 7, (v // 7) % 7, v % 7):   # base-7, MSD first
            c = DIGIT_TO_COLOR[digit]
            enc.append('W' if c == enc[-1] else c)           # break repeats
    return ''.join(enc)


# --------------------------------------------------------------------------- #
# 4. disassemble(colors) -> list of program bytes  (port of FlashDasm)
# --------------------------------------------------------------------------- #

def _remove_whites(colors):
    """Undo the repeat->White substitution: W means 'repeat previous letter'."""
    out = []
    prev = 'W'
    for c in colors:
        cur = prev if c == 'W' else c
        out.append(cur)
        prev = cur
    return out


def decode_values(colors):
    """Color string -> full list of framed integer values (markers included)."""
    # encode() (like JS blink) prefixes one neutral leading White that is not
    # data; FlashDasm-style input has none. Drop a single leading White so the
    # first real color seeds _remove_whites correctly.
    if colors.startswith('W'):
        colors = colors[1:]
    letters = _remove_whites(colors)
    values = []
    for j in range(0, len(letters) - 2, 3):
        d2 = COLOR_TO_DIGIT[letters[j]]
        d1 = COLOR_TO_DIGIT[letters[j + 1]]
        d0 = COLOR_TO_DIGIT[letters[j + 2]]
        values.append(d2 * 49 + d1 * 7 + d0)
    return values


def disassemble(colors, raw=False):
    """Decode a color string to program bytes.

    Default: strip frame head (3) + version (2) + length (3) = 8 leading values
    and the trailing checksum + tail marker, returning just the program bytes.
    raw=True mirrors FlashDasm: skip 8, keep the trailing checksum + 334.
    """
    values = decode_values(colors)
    body = values[8:]
    if not raw and len(body) >= 2:
        body = body[:-2]   # drop checksum + FRAME_TAIL
    return body


# letter -> 24-bit RGB, for the terminal flasher's truecolor background.
COLOR_RGB = {
    'K': (0, 0, 0), 'R': (255, 0, 0), 'G': (0, 255, 0), 'B': (0, 0, 255),
    'C': (0, 255, 255), 'M': (255, 0, 255), 'Y': (255, 255, 0),
    'W': (255, 255, 255),
}

# Terminal flasher paints a fixed block sized to look ~square to the robot's
# sensor. Terminal cells are ~2:1 tall, so 64 cols x 32 rows reads as a square.
BLOCK_ROWS = 8
BLOCK_COLS = 16


def flash_terminal(colors, interval_ms=50, prompt=True):
    """Flash a fixed ~square block of the terminal, 50ms/frame (20Hz).

    Works on WSL/headless setups. Uses 24-bit truecolor so the colors are exact
    regardless of the terminal theme. Only a
    BLOCK_COLS x BLOCK_ROWS block is painted (on a black backdrop) so surrounding
    terminal contents don't bleed color into the sensor.
    """
    import time
    import shutil

    cols, rows = shutil.get_terminal_size((80, 24))
    row0 = max(0, (rows - BLOCK_ROWS) // 2) + 1   # 1-based; center vertically
    col0 = max(0, (cols - BLOCK_COLS) // 2) + 1   # 1-based; center horizontally
    line = ' ' * BLOCK_COLS

    def clear_black():
        # black backdrop for the whole screen, drawn once.
        sys.stdout.write('\033[48;2;0;0;0m\033[2J\033[H')
        sys.stdout.flush()

    def fill(letter):
        r, g, b = COLOR_RGB[letter]
        # set truecolor bg, then paint the block row by row (one write/frame).
        buf = ['\033[48;2;%d;%d;%dm' % (r, g, b)]
        for i in range(BLOCK_ROWS):
            buf.append('\033[%d;%dH%s' % (row0 + i, col0, line))
        sys.stdout.write(''.join(buf))
        sys.stdout.flush()

    def draw_border():
        # solid white square filling the flash block, so the user can see exactly
        # where to place the robot before pressing ENTER.
        fill('W')

    hide_cursor, show_cursor, reset = '\033[?25l', '\033[?25h', '\033[0m'
    try:
        clear_black()
        draw_border()
        if prompt:
            try:
                input()   # wait for ENTER; the white square shows the flash area
            except EOFError:
                pass  # non-interactive stdin: just proceed
        clear_black()     # erase the preview square before flashing
        sys.stdout.write(hide_cursor)
        sys.stdout.flush()
        for c in colors:
            fill(c)
            time.sleep(interval_ms / 1000.0)
        fill('W')
        time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(show_cursor + reset + '\033[2J\033[H')
        sys.stdout.flush()


# --------------------------------------------------------------------------- #
# 6. self-test (golden vectors from FlashAsm tests())
# --------------------------------------------------------------------------- #

DEFAULT_PROGRAM = ('127 0 0 led  100 wait   0 127 0 led  100 wait   '
                   '0 0 127 led  100 wait   OFF end')

_GOLDEN = [
    ([45, 36, 147, 0, 0, 0, 184, 0, 30, 147, 0, 174],
     "CRYCYMCRWKWRKWYBRBKWKWRMKCYKMRYKWKWKWKWKWKYMGKWKWBGYKWKWKYWCKMYCMW"),
    ([45, 36, 147, 127, 0, 0, 184, 100, 155, 0, 127, 0, 184, 100, 155,
      0, 0, 127, 184, 100, 155, 0, 174],
     "CRYCYMCRWKWRKWYBKWKWKWYGKCYKMRYKWGBRKWKWKWYMGWKGYRWKWKGBRKWKYMGWKGYRWKWKWKWGBRYMGWKGYRWKWKYWCBMCWMW"),
]


def _self_test():
    # FlashAsm omits the leading White; compare against encode(...)[1:].
    for prog, expected in _GOLDEN:
        got = encode(prog, evo=False)[1:]
        assert got == expected, (
            'golden vector mismatch\n  expected %s\n  got      %s' % (expected, got))
    # Round-trip the default program: assemble -> encode -> disassemble.
    code, evo = assemble(DEFAULT_PROGRAM)
    colors = encode(code, evo)
    back = disassemble(colors)
    assert back == code, ('round-trip mismatch\n  code %s\n  back %s'
                          % (code, back))

    # Ground-truth checks (OZOBLOCKLY_VM.md).
    # Negative literals: -127 -> (|i|-1) ~  == 7e 83, not 7e 8b (neg).
    assert assemble('-127')[0] == [0x7e, 0x83], 'negative-literal encoding'
    # Evo audio operand order: 60 note 50 0 beep -> 3c cb 32 00 ca.
    assert assemble('60 note 50 0 beep')[0] == [0x3c, 0xcb, 0x32, 0x00, 0xca], 'audio'
    # Capability byte V is the max instruction-bank used.
    assert envelope(assemble('127 0 0 led')[0])[0] == 1, 'V bank1'
    assert envelope(assemble('REG_BATTERY readReg')[0])[0] == 2, 'V bank2 (readReg)'
    assert envelope(assemble('1 2 write-file')[0])[0] == 7, 'V bank7 (write-file)'
    # Non-blocking audio stays at bank 1 (the Evo shuts down if it claims V=5).
    assert envelope(assemble('60 note 50 0 beep')[0])[0] == 1, 'V bank1 (note/beep)'
    # Deprecated `play-file` (0xc8) must refuse to compile.
    try:
        assemble('evo 0 0 0 1 1 play-file end')
        raise AssertionError('play-file should be a compile error')
    except AssembleError:
        pass
    # A ':' inside a comment must not trigger the colon-def jump prefix.
    assert assemble('\\ a : b\n5')[0] == [5], 'comment-colon prefix'
    # Round-trip a program exercising the new opcodes.
    ev_code, ev = assemble('evo  64 note 30 0 beep  REG_BATTERY readReg drop  OFF end')
    assert disassemble(encode(ev_code, ev)) == ev_code, 'evo round-trip'
    print('selftest OK: 2 golden vectors + round-trip + ground-truth checks passed')


# --------------------------------------------------------------------------- #
# 7. CLI
# --------------------------------------------------------------------------- #

def _read_source(arg):
    """Read FlashForth source from a file path, '-'/stdin, or literal text."""
    if arg is None:
        return DEFAULT_PROGRAM
    if arg == '-':
        return sys.stdin.read()
    try:
        with open(arg, 'r') as f:
            return f.read()
    except (OSError, IOError):
        return arg   # treat the argument itself as source text


def _hex_bytes(code):
    return ' '.join('%02x' % b for b in code)


def cmd_compile(args):
    src = _read_source(args.source)
    code, evo = assemble(src)
    colors = encode(code, evo)
    if args.colors_only:
        print(colors)
    else:
        print('mode:     %s' % ('Evo' if evo else 'Bit'))
        print('bytecode: %s' % _hex_bytes(code))
        print('colors:   %s' % colors)
    if args.out:
        with open(args.out, 'w') as f:
            f.write(colors + '\n')
        if not args.colors_only:
            print('wrote %s' % args.out)
    return 0


def cmd_flash(args):
    src = _read_source(args.source)
    code, evo = assemble(src)
    colors = encode(code, evo)
    print('Flashing %d frames (%s mode).'
          % (len(colors), 'Evo' if evo else 'Bit'))
    flash_terminal(colors)
    return 0


def cmd_disasm(args):
    colors = _read_source(args.colors).strip()
    body = disassemble(colors, raw=args.raw)
    if args.hex_only:
        print(_hex_bytes(body))
    else:
        for b in body:
            name = _OPCODE_NAME.get(b)
            if b < 0x80:
                print('%02x    %d' % (b, b))
            else:
                print('%02x    %s' % (b, name if name else '?'))
    return 0


def cmd_selftest(_args):
    _self_test()
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog='ozobot',
        description='Compile / flash / disassemble Ozobot FlashForth programs.')
    sub = p.add_subparsers(dest='cmd', required=True)

    c = sub.add_parser('compile', help='compile source to bytecode + colors')
    c.add_argument('source', nargs='?',
                   help='.ff file, "-" for stdin, or literal source '
                        '(default: built-in blink demo)')
    c.add_argument('--colors-only', action='store_true',
                   help='print only the color string')
    c.add_argument('--out', help='also write the color string to this file')
    c.set_defaults(func=cmd_compile)

    f = sub.add_parser('flash', help='compile and flash to the robot')
    f.add_argument('source', nargs='?',
                   help='.ff file, "-" for stdin, or literal source')
    f.set_defaults(func=cmd_flash)

    d = sub.add_parser('disasm', help='decode a color string to bytecode')
    d.add_argument('colors', nargs='?', default='-',
                   help='color string, a file, or "-" for stdin')
    d.add_argument('--raw', action='store_true',
                   help='mirror FlashDasm (keep trailing checksum + marker)')
    d.add_argument('--hex-only', action='store_true',
                   help='print bytes on one line')
    d.set_defaults(func=cmd_disasm)

    s = sub.add_parser('selftest', help='run golden-vector + round-trip checks')
    s.set_defaults(func=cmd_selftest)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except AssembleError as exc:
        print('error: %s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
