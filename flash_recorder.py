#!/usr/bin/env python3
"""flash_recorder.py -- sample the screen color under the mouse and record Ozobot
flash-code color sequences from the OzoBlockly web tool (or any 20Hz flasher).

Hover the mouse over the middle of the flashing pad and leave it there. This tool
samples the pixel under the cursor at a high rate, classifies each frame into one
of Ozobot's 8 colors -- K R G B C M Y W -- and logs the sequence of ~50ms flashes.
White (W) is a real frame here ("repeat last color"); the sequence is logged
verbatim, exactly as `ozobot.py disasm` wants it:

    python3 ozobot.py disasm "WCRYCYM..."

The robot decodes TRANSITIONS, not the color on a fixed per-frame clock -- that's
why the encoder never shows the same color twice in a row and uses White as an
explicit "repeat" transition. This recorder works the same way: it logs a new
letter only when the color CHANGES (a stable transition), so the captured sequence
is exactly what the robot sees. Timing is used only to debounce mid-transition
blended pixels and to strip idle -- never to read data.

Controls: just watch. A capture *segment* is emitted automatically once the pad
goes idle (no change for --idle-ms). Press Ctrl-C to print everything and quit.

Deps: one screen grabber -- `pip install mss` (preferred) or Pillow. Mouse position
uses the OS (ctypes on Windows) or falls back to pynput/pyautogui if installed.

WSL NOTE: run this with the Python that can SEE the browser. If OzoBlockly is in a
Windows browser, run flash_recorder.py under **Windows** Python (py.exe), not inside
WSL -- a WSL interpreter cannot capture the Windows screen or read the Windows mouse.
"""

import argparse
import sys
import time

# --------------------------------------------------------------------------- #
# Color classification: threshold each channel, map the 3 bits to a letter.
# bits = (r>thr, g>thr, b>thr).  Robust for the saturated colors a screen emits.
# --------------------------------------------------------------------------- #
BITS_TO_LETTER = {
    (0, 0, 0): 'K', (1, 0, 0): 'R', (0, 1, 0): 'G', (0, 0, 1): 'B',
    (0, 1, 1): 'C', (1, 0, 1): 'M', (1, 1, 0): 'Y', (1, 1, 1): 'W',
}


def classify(rgb, thr):
    r, g, b = rgb
    return BITS_TO_LETTER[(int(r > thr), int(g > thr), int(b > thr))]


LETTER_TO_RGB = {'K': (0, 0, 0), 'R': (255, 0, 0), 'G': (0, 255, 0),
                 'B': (0, 0, 255), 'C': (0, 255, 255), 'M': (255, 0, 255),
                 'Y': (255, 255, 0), 'W': (255, 255, 255)}

# Every compiled stream opens with the frame head (values 304 320 302), which
# renders on screen as these 9 colors after white-substitution. Recording waits
# until this exact run of changes is seen, then starts the captured sequence here
# (which is precisely where ozobot.disassemble expects position 0). A leading
# neutral White may precede it on screen; it is not needed and is ignored.
START_MARKER = 'CRYCYMCRW'


# --------------------------------------------------------------------------- #
# Backends: mouse position + a small averaged screen sample under the cursor.
# --------------------------------------------------------------------------- #
def _make_mouse_backend():
    """Return a callable -> (x, y) in absolute screen pixels."""
    if sys.platform.startswith('win'):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        def pos():
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            return pt.x, pt.y
        return pos, 'ctypes(win32)'
    try:
        from pynput.mouse import Controller
        ctrl = Controller()

        def pos():
            x, y = ctrl.position
            return int(x), int(y)
        return pos, 'pynput'
    except Exception:
        pass
    try:
        import pyautogui

        def pos():
            p = pyautogui.position()
            return int(p[0]), int(p[1])
        return pos, 'pyautogui'
    except Exception:
        raise SystemExit(
            'No way to read the mouse position. Install one of:  pip install pynput\n'
            '(On Windows this should not happen -- ctypes is built in.)')


def _make_grab_backend(box):
    """Return a callable (x, y) -> averaged (r, g, b) over a box*box region."""
    half = box // 2
    try:
        import mss
        sct = mss.mss()

        def grab(x, y):
            mon = {'left': x - half, 'top': y - half, 'width': box, 'height': box}
            img = sct.grab(mon)
            raw = img.rgb                      # bytes, RGBRGB...
            n = max(1, len(raw) // 3)
            r = sum(raw[0::3]) // n
            g = sum(raw[1::3]) // n
            b = sum(raw[2::3]) // n
            return r, g, b
        return grab, 'mss'
    except Exception:
        pass
    try:
        from PIL import ImageGrab

        def grab(x, y):
            im = ImageGrab.grab(bbox=(x - half, y - half, x - half + box,
                                      y - half + box))
            px = list(im.getdata())
            n = len(px)
            r = sum(p[0] for p in px) // n
            g = sum(p[1] for p in px) // n
            b = sum(p[2] for p in px) // n
            return r, g, b
        return grab, 'PIL.ImageGrab'
    except Exception:
        raise SystemExit(
            'No screen-grab backend. Install one:  pip install mss   (or Pillow)')


# --------------------------------------------------------------------------- #
# Simulated backend: replay a color string at 20Hz, then idle, so the whole
# detect->segment->disasm pipeline can be exercised without a screen.
# --------------------------------------------------------------------------- #
def _make_sim_backends(colors, frame_ms=50.0):
    start = time.monotonic()
    span = len(colors) * frame_ms / 1000.0

    def grab(x, y):
        t = time.monotonic() - start
        if t < 0 or t >= span:
            return (255, 255, 255)          # idle white before/after the flash
        return LETTER_TO_RGB[colors[int(t / (frame_ms / 1000.0))]]
    return (lambda: (0, 0)), grab, 'sim', 'sim(%d frames)' % len(colors)


# --------------------------------------------------------------------------- #
# Optional: decode a captured string immediately using the sibling ozobot.py.
# --------------------------------------------------------------------------- #
def try_disasm(colors):
    try:
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import ozobot
        body = ozobot.disassemble(colors)
        return ' '.join('%02x' % b for b in body)
    except Exception as exc:
        return 'disasm unavailable (%s)' % exc


# --------------------------------------------------------------------------- #
# Main capture loop.
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--threshold', type=int, default=100,
                    help='per-channel on/off threshold 0-255 (default 100)')
    ap.add_argument('--box', type=int, default=7,
                    help='sample a box*box pixel patch under the cursor (default 7)')
    ap.add_argument('--stable-ms', type=float, default=18.0,
                    help='a color must hold this long to count as a frame (default 18)')
    ap.add_argument('--idle-ms', type=float, default=900.0,
                    help='no change for this long ends a capture segment (default 900)')
    ap.add_argument('--min-frames', type=int, default=12,
                    help='ignore segments shorter than this many frames (default 12)')
    ap.add_argument('--no-wait-start', action='store_true',
                    help='record every change immediately; do NOT wait for the start frame')
    ap.add_argument('--rate', type=float, default=400.0,
                    help='sampling rate in Hz (default 400)')
    ap.add_argument('--simulate', metavar='COLORS',
                    help='no screen -- play COLORS at 20Hz through the pipeline (self-test/demo)')
    args = ap.parse_args()

    if args.simulate:
        mouse_pos, grab, mbk, gbk = _make_sim_backends(args.simulate)
    else:
        mouse_pos, mbk = _make_mouse_backend()
        grab, gbk = _make_grab_backend(args.box)
    period = 1.0 / args.rate
    stable = args.stable_ms / 1000.0
    idle = args.idle_ms / 1000.0
    wait_start = not args.no_wait_start

    print('flash_recorder  |  mouse=%s  grab=%s  thr=%d  box=%d' %
          (mbk, gbk, args.threshold, args.box))
    if wait_start:
        print('Hover over the flashing pad. Recording ARMS and waits for the start '
              'frame (%s), then captures until idle. Ctrl-C to finish.\n' % START_MARKER)
    else:
        print('Hover over the flashing pad. Recording every change (no start gate). '
              'Ctrl-C to finish.\n')

    # State machine: WAITING (armed, watching the change-stream for START_MARKER)
    # -> RECORDING (appending changes) -> emit on idle -> back to WAITING.
    cand = None                 # current candidate color (not yet confirmed stable)
    cand_since = 0.0
    last_stable = None          # last confirmed stable color
    recent = ''                 # tail of the recent change-stream, for marker match
    recording = not wait_start  # if not gating, record from the first change
    seg = []
    last_change = time.monotonic()

    def emit_segment():
        nonlocal seg, recording
        s = ''.join(seg)
        n = len(seg)
        seg = []
        recording = not wait_start
        if n < args.min_frames:
            if n:
                print('\n(ignored short segment: %d frames  %s)\n' % (n, s))
            return
        print('\n--- captured segment: %d frames ---' % n)
        print('colors: %s' % s)
        print('bytes : %s' % try_disasm(s))
        print('        feed to: python3 ozobot.py disasm "%s"\n' % s)

    try:
        while True:
            t0 = time.monotonic()
            x, y = mouse_pos()
            try:
                rgb = grab(x, y)
            except Exception:
                time.sleep(period)
                continue
            letter = classify(rgb, args.threshold)

            # confirm a stable color change
            if letter != cand:
                cand = letter
                cand_since = t0
            elif cand != last_stable and (t0 - cand_since) >= stable:
                last_stable = cand
                last_change = t0
                if not recording:
                    # WAITING: feed the change into the rolling marker window
                    recent = (recent + cand)[-len(START_MARKER):]
                    if recent == START_MARKER:
                        recording = True
                        recent = ''
                        seg = list(START_MARKER)   # seq begins at the frame head
                        sys.stdout.write('\r' + ' ' * 72 + '\r')
                        print('[start frame detected] recording...')
                else:
                    # RECORDING: append every subsequent change
                    seg.append(cand)

            # idle -> emit and re-arm
            if recording and seg and (t0 - last_change) >= idle:
                emit_segment()
                if args.simulate:            # self-test/demo: stop after one segment
                    raise KeyboardInterrupt

            # live status line
            if recording:
                tag = 'REC %3d' % len(seg)
            elif wait_start:
                tag = 'ARMED  '
            else:
                tag = 'idle   '
            sys.stdout.write('\r[%s] cur=%s rgb=(%3d,%3d,%3d) match=%-9s '
                             % (tag, letter, rgb[0], rgb[1], rgb[2],
                                recent if wait_start and not recording else ''))
            sys.stdout.flush()

            dt = period - (time.monotonic() - t0)
            if dt > 0:
                time.sleep(dt)
    except KeyboardInterrupt:
        if recording and seg:
            emit_segment()
        print('\nbye.')


if __name__ == '__main__':
    main()
