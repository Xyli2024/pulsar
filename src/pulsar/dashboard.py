"""dashboard.py — live TUI dashboard using rich."""

import math
import os
import queue
import random
import select
import shutil
import signal
import sys
import threading
import time

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .collector import SystemInfo, Snapshot, collect
from . import facts as _facts

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MILESTONES = [
    (5 * 60,  "5 minutes in. You're basically a sysadmin now."),
    (15 * 60, "Still here? Your code is not going to optimize itself."),
    (30 * 60, "30 minutes. Have you tried turning it off and on again?"),
    (42 * 60, "42 minutes. The answer, apparently, is not in this dashboard."),
    (60 * 60, "1 hour. At this point, you live here. We've prepared a small room."),
]


DISCO_COLORS = [
    "red", "green", "yellow", "blue", "magenta", "cyan",
    "bright_red", "bright_green", "bright_yellow",
    "bright_blue", "bright_magenta", "bright_cyan",
]

# Fireworks animation
_FW_TOTAL_FRAMES   = 25    # frames per show
_FW_FRAME_DURATION = 0.08  # seconds per frame  (~2 s total)
_FW_BRIGHT = ["✦", "★", "✸", "✺", "*", "+"]
_FW_DIM    = ["·", "°", ".", "｡", "˙"]

# Raw ANSI codes for the sparkle overlay (bypasses Rich, written directly to
# the terminal file so the dashboard underneath is left intact)
_ANSI_FG = {
    "red":            "\033[31m",  "green":          "\033[32m",
    "yellow":         "\033[33m",  "blue":           "\033[34m",
    "magenta":        "\033[35m",  "cyan":           "\033[36m",
    "bright_red":     "\033[91m",  "bright_green":   "\033[92m",
    "bright_yellow":  "\033[93m",  "bright_blue":    "\033[94m",
    "bright_magenta": "\033[95m",  "bright_cyan":    "\033[96m",
}
_ANSI_BOLD = "\033[1m"
_ANSI_DIM  = "\033[2m"
_ANSI_RST  = "\033[0m"

# Fact bar animation
_TYPING_SECS    = 20.0   # target duration to type full body text
_STABLE_SECS    = 10.0   # static display time after typing completes
_FADE_PALETTE   = ["white", "grey74", "grey54", "grey39", "grey27", "grey15", "grey7"]
_FADE_STEP_SECS = 0.125  # seconds per fade step  (7 × 0.125 ≈ 875 ms total)
_LOOP_TICK      = 0.10   # main loop tick; collect() is gated separately by interval
_MIN_CHARS_SEC  = 9.0    # slowest allowed typing speed (chars / sec)
_MAX_CHARS_SEC  = 15.0   # fastest allowed typing speed (chars / sec)


def _fact_char_interval(text: str) -> float:
    """Seconds per character so that len(text) chars type in ~_TYPING_SECS."""
    chars_per_sec = max(_MIN_CHARS_SEC, min(_MAX_CHARS_SEC, len(text) / _TYPING_SECS))
    return 1.0 / chars_per_sec


# ---------------------------------------------------------------------------
# Keyboard — background thread
# ---------------------------------------------------------------------------

def _start_keyboard_thread(key_queue: queue.Queue, stop_event: threading.Event) -> None:
    if not sys.stdin.isatty():
        return

    def _worker():
        try:
            import termios, tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                # setcbreak: immediate keystrokes, OPOST preserved, ISIG preserved
                tty.setcbreak(fd)
                while not stop_event.is_set():
                    if select.select([sys.stdin], [], [], 0.05)[0]:
                        # os.read bypasses Python's TextIOWrapper buffer so that
                        # the full escape sequence (e.g. \x1b[A) is captured in
                        # one shot — reading via sys.stdin.read(1) would drain
                        # only the ESC byte while leaving [A in Python's buffer,
                        # causing select() to see no data and drop the sequence.
                        data = os.read(fd, 32)
                        i = 0
                        while i < len(data):
                            b = data[i]
                            if b == 0x1b:  # ESC — look ahead for CSI sequence
                                if i + 2 < len(data) and data[i + 1] == ord("["):
                                    if data[i + 2] == ord("A"):
                                        key_queue.put("UP")
                                        i += 3
                                        continue
                                    elif data[i + 2] == ord("B"):
                                        key_queue.put("DOWN")
                                        i += 3
                                        continue
                                key_queue.put("ESC")  # lone ESC — cancel kill mode
                                i += 1
                            else:
                                key_queue.put(chr(b))
                                i += 1
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _bar(pct: float, width: int = 20) -> str:
    filled = max(0, min(int(pct / 100 * width), width))
    return "█" * filled + "░" * (width - filled)


def _color(pct: float, disco: bool = False) -> str:
    if disco:
        return random.choice(DISCO_COLORS)
    return "green" if pct < 50 else "yellow" if pct < 80 else "red"


def _mem_color(pct: float, disco: bool = False) -> str:
    if disco:
        return random.choice(DISCO_COLORS)
    return "green" if pct < 70 else "yellow" if pct < 90 else "red"


# ---------------------------------------------------------------------------
# Fireworks animation
# ---------------------------------------------------------------------------

def _make_burst_centers(cols: int, rows: int, count: int = 3) -> list[tuple]:
    """Pick random burst origins, each with its own color."""
    return [
        (
            random.randint(cols // 6, 5 * cols // 6),
            random.randint(rows // 6, 2 * rows // 3),
            random.choice(DISCO_COLORS),
        )
        for _ in range(count)
    ]


def _draw_sparkle_overlay(
    out, frame: int, cols: int, rows: int, bursts: list[tuple]
) -> None:
    """
    Draw one fireworks frame as an ANSI-positioned overlay on top of whatever
    Rich has already rendered.  The dashboard underneath is untouched; the
    next live.update() wipes the overlay automatically.

    Glow effect: each leading-edge spark gets bold+bright colour; its four
    cardinal neighbours receive a dim halo in the same colour.
    """
    progress   = frame / _FW_TOTAL_FRAMES
    max_radius = min(cols * 0.45, rows * 0.85)
    cur_radius = progress * max_radius

    # (col, row) → (char, ansi_prefix)
    grid: dict[tuple[int, int], tuple[str, str]] = {}

    for cx, cy, color in bursts:
        fg = _ANSI_FG.get(color, "")

        # Leading edge — bold bright chars
        for deg in range(0, 360, 4):
            rad = math.radians(deg)
            x = int(cx + cur_radius * math.cos(rad))
            y = int(cy + cur_radius * math.sin(rad) * 0.5)
            if 1 <= x < cols - 1 and 1 <= y < rows - 1 and random.random() < 0.75:
                grid[(x, y)] = (random.choice(_FW_BRIGHT), _ANSI_BOLD + fg)
                # Glow halo — dim neighbours
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    hx, hy = x + dx, y + dy
                    if (1 <= hx < cols - 1 and 1 <= hy < rows - 1
                            and (hx, hy) not in grid
                            and random.random() < 0.6):
                        grid[(hx, hy)] = (random.choice(_FW_DIM), fg)

        # Trailing sparks — dimmer inner rings
        for trail in range(1, 5):
            r = cur_radius - trail * 2.5
            if r <= 0:
                continue
            for deg in range(0, 360, 8):
                rad = math.radians(deg)
                x = int(cx + r * math.cos(rad))
                y = int(cy + r * math.sin(rad) * 0.5)
                if (1 <= x < cols - 1 and 1 <= y < rows - 1
                        and (x, y) not in grid
                        and random.random() < 0.40):
                    grid[(x, y)] = (random.choice(_FW_DIM), _ANSI_DIM + fg)

    # Fade: randomly drop sparks in the last 40 % of the animation
    if progress > 0.6:
        fade = (progress - 0.6) / 0.4
        grid = {k: v for k, v in grid.items() if random.random() > fade}

    # Write to terminal using ANSI cursor-addressing
    buf = ["\033[s"]  # save cursor position
    for (col, row), (char, prefix) in grid.items():
        buf.append(f"\033[{row + 1};{col + 1}H{prefix}{char}{_ANSI_RST}")
    buf.append("\033[u")  # restore cursor position
    out.write("".join(buf))
    out.flush()


# ---------------------------------------------------------------------------
# Dashboard panel builders
# ---------------------------------------------------------------------------

def _build_header(info: SystemInfo, interval: float, disco: bool = False) -> Panel:
    c = random.choice(DISCO_COLORS) if disco else "cyan"
    t = Text()
    t.append("pulsar", style=f"bold {c}")
    t.append(
        f"  •  {info.cpu_model}  •  {info.cpu_cores} cores"
        f"  •  {info.cpu_freq_current} / {info.cpu_freq_max} GHz max\n",
        style="white",
    )
    t.append(
        f"GPU: {info.gpu_model}  •  RAM: {info.ram_total} GB"
        f"  •  refresh: {interval}s"
        f"  •  [dim]k[/dim] kill (SIGTERM)  •  [dim]K[/dim] force kill (SIGKILL)"
        f"  •  [dim]y[/dim] fireworks  •  [dim]q[/dim] quit",
        style="dim",
    )
    return Panel(t)


def _build_cpu_text(snap: Snapshot, disco: bool = False) -> Text:
    t = Text()
    n = len(snap.cpu_per_core)

    has_freq = len(snap.cpu_freq_per_core) == n
    if n <= 8:
        for i, pct in enumerate(snap.cpu_per_core):
            c = _color(pct, disco)
            t.append(f"Core {i}  ", style="dim")
            t.append(_bar(pct), style=c)
            t.append(f"  {pct:5.1f}%", style=c)
            if has_freq:
                t.append(f"  {snap.cpu_freq_per_core[i]:.2f} GHz", style="dim")
            t.append("\n")
        t.append("─" * (44 if has_freq else 34) + "\n", style="dim")
        c = _color(snap.cpu_avg, disco)
        t.append("avg     ", style="dim")
        t.append(_bar(snap.cpu_avg), style=c)
        t.append(f"  {snap.cpu_avg:5.1f}%", style=f"bold {c}")
    else:
        c = _color(snap.cpu_avg, disco)
        t.append(f"avg {snap.cpu_avg:.1f}%  ", style=f"bold {c}")
        t.append(_bar(snap.cpu_avg) + "\n\n", style=c)
        for row_start in range(0, n, 4):
            row = snap.cpu_per_core[row_start: row_start + 4]
            for j in range(len(row)):
                t.append(f"  {row_start + j:>3}  ", style="dim")
            t.append("\n")
            for pct in row:
                t.append(f" {pct:5.1f}%", style=_color(pct, disco))
            t.append("\n\n")
    return t


def _build_stats_text(snap: Snapshot, disco: bool = False) -> Text:
    t = Text()
    mc = _mem_color(snap.mem_percent, disco)
    t.append("Memory\n", style="bold")
    t.append(f"  {snap.mem_used} / {snap.mem_total} GB  ({snap.mem_percent:.1f}%)\n", style=mc)
    t.append("  " + _bar(snap.mem_percent) + "\n\n", style=mc)
    dc = random.choice(DISCO_COLORS) if disco else "white"
    t.append("Disk I/O\n", style="bold")
    t.append(f"  Read   {snap.disk_read_mbps:7.2f} MB/s\n", style=dc)
    t.append(f"  Write  {snap.disk_write_mbps:7.2f} MB/s\n\n", style=dc)
    nc = random.choice(DISCO_COLORS) if disco else "cyan"
    t.append("Network I/O\n", style="bold")
    t.append(f"  ↓ Recv  {snap.net_recv_mbps:7.2f} MB/s\n", style=nc)
    t.append(f"  ↑ Sent  {snap.net_sent_mbps:7.2f} MB/s", style=nc)
    return t


def _build_fact_bar(fact: dict, char_pos: int, anim_state: str,
                    fade_step: int = 0) -> Panel:
    tag_color = fact.get("color", "white")
    body      = fact["text"]
    t = Text()
    t.append("  ")

    if anim_state == "fading":
        fade_color = _FADE_PALETTE[min(fade_step, len(_FADE_PALETTE) - 1)]
        t.append(fact["tag"], style=f"bold {fade_color}")
        t.append("  ")
        t.append(body, style=fade_color)
    elif anim_state == "typing":
        t.append(fact["tag"], style=f"bold {tag_color}")
        t.append("  ")
        t.append(body[:char_pos], style="white")
        if char_pos < len(body):
            pac = "ᗧ" if char_pos % 2 == 0 else "ᗦ"
            t.append(pac, style="bold yellow")
    else:  # stable
        t.append(fact["tag"], style=f"bold {tag_color}")
        t.append("  ")
        t.append(body, style="white")

    return Panel(t, border_style="dim")


def _build_kill_prompt(kill_input: str, sig: signal.Signals) -> Panel:
    sig_name  = "SIGKILL" if sig == signal.SIGKILL else "SIGTERM"
    sig_color = "red"     if sig == signal.SIGKILL else "yellow"
    t = Text()
    t.append("  ")
    t.append(sig_name, style=f"bold {sig_color}")
    t.append("  PID or process name:  ")
    t.append(kill_input, style="bold white")
    t.append("█", style="dim")
    t.append("\n  ")
    t.append("Enter", style="dim")
    t.append(" confirm  •  ")
    t.append("Esc", style="dim")
    t.append(" cancel")
    return Panel(t, title="Kill Process", border_style=sig_color)


def _build_renderable(info: SystemInfo, snap: Snapshot, interval: float,
                      milestone_msg: str | None = None,
                      disco: bool = False,
                      kill_prompt: Panel | None = None,
                      fact: dict | None = None,
                      fact_char_pos: int = 0,
                      fact_anim_state: str = "stable",
                      fact_fade_step: int = 0) -> Group:
    """Compose the full dashboard as a Rich renderable Group."""
    header = _build_header(info, interval, disco)

    grid = Table.grid(expand=True)
    grid.add_column(ratio=3)
    grid.add_column(ratio=2)
    grid.add_row(
        Panel(_build_cpu_text(snap, disco),   title="CPU",    border_style="dim"),
        Panel(_build_stats_text(snap, disco), title="System", border_style="dim"),
    )

    proc_table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 1))
    proc_table.add_column("PID",  justify="right", width=7)
    proc_table.add_column("Name", width=26)
    proc_table.add_column("CPU%", justify="right", width=6)
    proc_table.add_column("MEM",  justify="right", width=10)
    for p in snap.top_procs:
        c = _color(p["cpu"], disco)
        mem_str = (f"{p['mem_mb']} MB" if p["mem_mb"] < 1024
                   else f"{p['mem_mb'] / 1024:.2f} GB")
        proc_table.add_row(
            str(p["pid"]), p["name"],
            f"[{c}]{p['cpu']}%[/{c}]", mem_str,
        )

    footer = Text(f"\n  ★  {milestone_msg}", style="bold yellow") if milestone_msg else Text()
    procs = Panel(Group(proc_table, footer), title="Top Processes", border_style="dim")

    fact_bar = (
        _build_fact_bar(fact, fact_char_pos, fact_anim_state, fact_fade_step)
        if fact else None
    )

    parts: list = [header, grid, procs]
    if kill_prompt is not None:
        parts.append(kill_prompt)
    if fact_bar is not None:
        parts.append(fact_bar)
    return Group(*parts)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(info: SystemInfo, interval: float = 1.0, top_n: int = 5,
        proc_filter: list[str] | None = None, disco: bool = False) -> None:
    """Run the live dashboard until the user quits."""

    console = Console()

    if not sys.stdout.isatty():
        console.print(
            "[bold red]Error:[/bold red] the live dashboard requires a real terminal.\n"
            "Use [bold]pulsar --once[/bold] for a single snapshot instead.",
            highlight=False,
        )
        return

    start_time          = time.monotonic()
    core_overload_since: dict[int, float] = {}
    seen_milestones: set = set()
    milestone_msg: str | None = None
    milestone_until     = 0.0

    # Kill-mode state
    kill_mode  = False
    kill_input = ""
    kill_sig   = signal.SIGTERM

    # Facts animation state
    _facts.start_online_fetch()
    fact_idx            = 0
    current_fact        = _facts.get_fact(0)
    milestone_override: dict | None = None  # when set, shown instead of current_fact
    fact_anim_state     = "typing"   # "typing" | "stable" | "fading"
    fact_char_pos       = 0
    fact_fade_step      = 0
    fact_char_int       = _fact_char_interval(current_fact["text"])
    fact_next_char_time = time.monotonic()
    fact_stable_start   = 0.0
    fact_next_fade_time = 0.0

    # Fireworks state — fw_frame >= _FW_TOTAL_FRAMES means "not playing"
    fw_frame  = _FW_TOTAL_FRAMES
    fw_bursts: list[tuple] = []

    key_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    _start_keyboard_thread(key_queue, stop_event)

    collect(top_n=top_n, proc_filter=proc_filter)
    time.sleep(min(interval, 0.3))
    snap = collect(top_n=top_n, proc_filter=proc_filter)  # initial reading
    last_collect_time = time.monotonic()

    with Live(console=console, screen=True, auto_refresh=False) as live:
        try:
            while True:
                now     = time.monotonic()
                elapsed = now - start_time

                prev_kill_mode  = kill_mode
                prev_kill_input = kill_input

                # --- Keyboard ---
                quit_requested = False
                while True:
                    try:
                        key = key_queue.get_nowait()
                    except queue.Empty:
                        break

                    if kill_mode:
                        if key in ("ESC", "\x1b"):
                            kill_mode = False
                            kill_input = ""
                        elif key in ("\r", "\n"):
                            # Execute kill
                            target = kill_input.strip()
                            if target:
                                try:
                                    pid = int(target)
                                    name = next((p["name"] for p in snap.top_procs
                                                 if p["pid"] == pid), "")
                                except ValueError:
                                    match = next((p for p in snap.top_procs
                                                  if target.lower() in p["name"].lower()), None)
                                    if match:
                                        pid, name = match["pid"], match["name"]
                                    else:
                                        pid, name = None, None
                                if pid is not None:
                                    sig_name = "SIGKILL" if kill_sig == signal.SIGKILL else "SIGTERM"
                                    try:
                                        os.kill(pid, kill_sig)
                                        milestone_msg = (f"Sent {sig_name} → PID {pid}"
                                                         + (f"  ({name})" if name else ""))
                                    except (ProcessLookupError, PermissionError) as e:
                                        milestone_msg = f"kill failed: {e}"
                                else:
                                    milestone_msg = f"No process matching '{target}'"
                                milestone_until = now + 4.0
                            kill_mode = False
                            kill_input = ""
                        elif key in ("\x7f", "\x08"):  # Backspace
                            kill_input = kill_input[:-1]
                        elif len(key) == 1 and 32 <= ord(key) <= 126:
                            kill_input += key
                    else:
                        if key in ("q", "Q", "\x03"):
                            quit_requested = True
                        elif key == "k":
                            kill_mode, kill_input, kill_sig = True, "", signal.SIGTERM
                        elif key == "K":
                            kill_mode, kill_input, kill_sig = True, "", signal.SIGKILL
                        elif key in ("y", "Y") and fw_frame >= _FW_TOTAL_FRAMES:
                            cols, rows = shutil.get_terminal_size((80, 24))
                            count      = random.randint(2, 6)
                            fw_bursts  = _make_burst_centers(cols, rows - 1, count)
                            fw_frame   = 0

                if quit_requested:
                    break

                # --- Fireworks frame (overlay — dashboard stays visible) ---
                if fw_frame < _FW_TOTAL_FRAMES:
                    cols, rows = shutil.get_terminal_size((80, 24))
                    # Refresh the dashboard first, then paint sparkles on top
                    live.update(
                        _build_renderable(info, snap, interval,
                                          milestone_msg=milestone_msg,
                                          disco=disco,
                                          fact=milestone_override or current_fact,
                                          fact_char_pos=fact_char_pos,
                                          fact_anim_state=fact_anim_state,
                                          fact_fade_step=fact_fade_step),
                        refresh=True,
                    )
                    _draw_sparkle_overlay(
                        live.console.file, fw_frame, cols, rows, fw_bursts
                    )
                    fw_frame += 1
                    time.sleep(_FW_FRAME_DURATION)
                    continue

                # --- Kill mode: only redraw when input changes, skip collect ---
                if kill_mode:
                    if kill_input != prev_kill_input or not prev_kill_mode:
                        live.update(
                            _build_renderable(info, snap, interval,
                                              milestone_msg=milestone_msg, disco=disco,
                                              kill_prompt=_build_kill_prompt(kill_input, kill_sig),
                                              fact=milestone_override or current_fact,
                                              fact_char_pos=fact_char_pos,
                                              fact_anim_state=fact_anim_state,
                                              fact_fade_step=fact_fade_step),
                            refresh=True,
                        )
                    time.sleep(0.05)
                    continue

                # --- Normal dashboard ---

                # If kill mode just ended, clear the prompt immediately
                if prev_kill_mode and not kill_mode:
                    live.update(
                        _build_renderable(info, snap, interval,
                                          milestone_msg=milestone_msg, disco=disco,
                                          fact=milestone_override or current_fact,
                                          fact_char_pos=fact_char_pos,
                                          fact_anim_state=fact_anim_state,
                                          fact_fade_step=fact_fade_step),
                        refresh=True,
                    )

                # Collect hardware data at most once per interval
                collected = False
                if now - last_collect_time >= interval:
                    snap = collect(top_n=top_n, proc_filter=proc_filter)
                    last_collect_time = now
                    collected = True

                    # Auto-trigger fireworks when a core pegs 100 % for 5+ seconds
                    for i, pct in enumerate(snap.cpu_per_core):
                        if pct >= 99.9:
                            core_overload_since.setdefault(i, now)
                            if (now - core_overload_since[i] >= 5.0
                                    and fw_frame >= _FW_TOTAL_FRAMES):
                                cols, rows = shutil.get_terminal_size((80, 24))
                                fw_bursts  = _make_burst_centers(cols, rows - 1,
                                                 random.randint(2, 6))
                                fw_frame   = 0
                        else:
                            core_overload_since.pop(i, None)

                    # Milestones → inject into fact bar as Pac-Man animation
                    if now >= milestone_until:
                        milestone_msg = None
                    for threshold, msg in MILESTONES:
                        if threshold not in seen_milestones and elapsed >= threshold:
                            seen_milestones.add(threshold)
                            milestone_override  = {"tag": "★", "color": "yellow", "text": msg}
                            fact_char_pos       = 0
                            fact_anim_state     = "typing"
                            fact_fade_step      = 0
                            fact_char_int       = _fact_char_interval(msg)
                            fact_next_char_time = now

                    if proc_filter and not snap.top_procs and "warned" not in seen_milestones:
                        seen_milestones.add("warned")
                        milestone_msg = f"No processes matched: {', '.join(proc_filter)}"
                        milestone_until = now + 5.0

                # Advance fact animation state machine; track whether a redraw is needed
                fact_changed = False
                if fact_anim_state == "typing":
                    if now >= fact_next_char_time:
                        fact_char_pos      += 1
                        fact_next_char_time = now + fact_char_int
                        fact_changed        = True
                        if fact_char_pos >= len(current_fact["text"]):
                            fact_anim_state   = "stable"
                            fact_stable_start = now

                elif fact_anim_state == "stable":
                    if now - fact_stable_start >= _STABLE_SECS:
                        fact_anim_state     = "fading"
                        fact_fade_step      = 0
                        fact_next_fade_time = now + _FADE_STEP_SECS
                        fact_changed        = True

                elif fact_anim_state == "fading":
                    if now >= fact_next_fade_time:
                        fact_fade_step     += 1
                        fact_next_fade_time = now + _FADE_STEP_SECS
                        fact_changed        = True
                        if fact_fade_step >= len(_FADE_PALETTE):
                            milestone_override  = None
                            fact_idx           += 1
                            current_fact        = _facts.get_fact(fact_idx)
                            fact_char_pos       = 0
                            fact_anim_state     = "typing"
                            fact_fade_step      = 0
                            fact_char_int       = _fact_char_interval(current_fact["text"])
                            fact_next_char_time = now

                # Redraw only when data refreshed or fact animation advanced;
                # this prevents disco mode from flickering at the loop tick rate.
                if collected or fact_changed:
                    live.update(
                        _build_renderable(info, snap, interval,
                                          milestone_msg=milestone_msg, disco=disco,
                                          fact=milestone_override or current_fact,
                                          fact_char_pos=fact_char_pos,
                                          fact_anim_state=fact_anim_state,
                                          fact_fade_step=fact_fade_step),
                        refresh=True,
                    )
                time.sleep(_LOOP_TICK)

        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()

    console.print("[dim]pulsar signing off. stay curious.[/dim]")
