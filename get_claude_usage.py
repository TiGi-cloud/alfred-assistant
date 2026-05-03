#!/usr/bin/env python3
"""Spawn claude CLI, run /usage, capture screen via pyte terminal emulator."""
import json, os, pty, re, select, time

def main():
    import pyte
    screen = pyte.Screen(120, 40)
    stream = pyte.Stream(screen)

    claude_path = os.path.expanduser('~/.local/bin/claude')
    env = os.environ.copy()
    for k in list(env):
        if 'CLAUDE' in k.upper():
            del env[k]
    env['NO_COLOR'] = '1'
    env['TERM'] = 'xterm-256color'
    env['COLUMNS'] = '120'
    env['LINES'] = '40'
    env['HOME'] = os.path.expanduser('~')
    env['PATH'] = os.environ.get('PATH', '/usr/bin:/bin') + ':' + os.path.expanduser('~/.local/bin')

    master, slave = pty.openpty()
    import subprocess
    try:
        proc = subprocess.Popen(
            [claude_path],
            stdin=slave, stdout=slave, stderr=slave,
            env=env, close_fds=True,
        )
    except Exception:
        os.close(slave)
        os.close(master)
        raise
    os.close(slave)

    def read_until(deadline):
        while time.time() < deadline:
            r, _, _ = select.select([master], [], [], 0.5)
            if r:
                try:
                    data = os.read(master, 8192)
                    stream.feed(data.decode('utf-8', errors='replace'))
                except OSError:
                    break

    # Wait for claude to start (welcome screen)
    read_until(time.time() + 8)

    # Send /usage and Enter (carriage return)
    os.write(master, b'/usage')
    time.sleep(1)
    os.write(master, b'\r')

    # Wait for usage panel to render
    read_until(time.time() + 8)

    # Capture screen
    lines = []
    for i in range(screen.lines):
        line = screen.buffer[i]
        if line:
            max_col = max(line.keys()) + 1 if line.keys() else 0
            text = ''.join(line[col].data if col in line else ' ' for col in range(max_col))
        else:
            text = ''
        lines.append(text.rstrip())
    screen_text = '\n'.join(lines)

    # Send /exit
    os.write(master, b'/exit\n')
    time.sleep(1)
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        proc.kill()
    try:
        os.close(master)
    except OSError:
        pass

    result = parse_usage(screen_text)
    print(json.dumps(result))

def fix_spacing(s):
    """Re-insert spaces that pyte may strip: 'Mar13at11am' -> 'Mar 13 at 11am'"""
    s = re.sub(r'([a-zA-Z])(\d)', r'\1 \2', s)
    s = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', s)
    s = re.sub(r'\(', ' (', s)
    return re.sub(r'\s+', ' ', s).strip()

def parse_usage(text):
    result = {}

    m = re.search(r'Current\s*session.*?(\d+)\s*%\s*used', text, re.DOTALL | re.IGNORECASE)
    if m:
        result['session_pct'] = int(m.group(1))
    m = re.search(r'Current\s*session.*?Resets?\s*(.+?)(?:\n|$)', text, re.DOTALL | re.IGNORECASE)
    if m:
        result['session_reset'] = fix_spacing(m.group(1).strip())

    # pyte may strip spaces, so use flexible whitespace matching
    m = re.search(r'Current\s*week\s*\(all\s*models?\).*?(\d+)\s*%\s*used', text, re.DOTALL | re.IGNORECASE)
    if m:
        result['week_pct'] = int(m.group(1))
    m = re.search(r'Current\s*week\s*\(all\s*models?\).*?Resets?\s*(.+?)(?:\n|$)', text, re.DOTALL | re.IGNORECASE)
    if m:
        result['week_reset'] = fix_spacing(m.group(1).strip())

    m = re.search(r'Current\s*week\s*\(Sonnet[^)]*\).*?(\d+)\s*%\s*used', text, re.DOTALL | re.IGNORECASE)
    if m:
        result['sonnet_pct'] = int(m.group(1))
    m = re.search(r'Current\s*week\s*\(Sonnet[^)]*\).*?Resets?\s*(.+?)(?:\n|$)', text, re.DOTALL | re.IGNORECASE)
    if m:
        result['sonnet_reset'] = fix_spacing(m.group(1).strip())

    if 'extra' in text.lower() and 'usage' in text.lower():
        result['extra_usage'] = 'notenabled' not in text.lower().replace(' ', '')

    if not any(k.endswith('_pct') for k in result):
        result['error'] = 'Could not parse usage output'
        result['raw'] = text[-600:]

    return result

if __name__ == '__main__':
    main()
