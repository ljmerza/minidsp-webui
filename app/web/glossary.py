"""The glossary: one plain explanation per term (phase 9 design §13).

One source for the "?" toggletips next to terms (the ``help("q")`` template global), the ``/help`` page and the guided
tasks' explanations. Each term has a short text (the toggletip, at most two sentences) and an optional longer one that
``/help`` shows after it. Copy rules: one word per thing (design §2), device facts from
``app/model/device_profile.py``, and nothing a newcomer has to look up elsewhere.
"""

import itertools
from collections.abc import Iterator
from dataclasses import dataclass, field

from markupsafe import Markup, escape


@dataclass(frozen=True, slots=True)
class Term:
    id: str  # also the anchor on /help (``/help#q``)
    label: str
    short: str
    more: str = ""
    group: str = ""
    related: tuple[str, ...] = field(default_factory=tuple)


# Groups in the order /help lists them.
GROUPS: tuple[str, ...] = (
    "Levels",
    "Shaping the sound",
    "Timing",
    "Routing and inputs",
    "Presets and profiles",
    "Sync and the device",
    "Health badges",
)


def _terms() -> Iterator[Term]:
    g = "Levels"
    yield Term(
        "volume",
        "Volume",
        "One volume for all outputs, applied after everything else; 0 dB is the maximum and −127 dB is near silence. "
        "Use it for everyday loudness and leave output levels for balance.",
        "Most listening sits roughly between −40 and −10 dB, shown as the shaded band; the right range depends on your "
        "source and amplifier gain, so treat it as a hint. Large drops ask first (Settings).",
        g,
        ("level", "mute"),
    )
    yield Term(
        "level",
        "Output level",
        "How loud this output is set, from −72 to +12 dB. It changes this output only, so use it to balance outputs "
        "against each other.",
        "It adds to the route level and the input level; the card's big number is that total.",
        g,
        ("route-level", "input-level", "total", "level-limit"),
    )
    yield Term(
        "route-level",
        "Route level",
        "How much of one input is sent to one output (one box in Routing). Route level and output level add up: a "
        "+4 dB route into a +12 dB output plays at +16 dB.",
        "Use it to go louder when the output is at its level limit, or to mix two inputs at different levels.",
        g,
        ("level", "routing", "total"),
    )
    yield Term(
        "input-level",
        "Input level",
        "Turns one input up or down before it is sent anywhere. It affects every output that input feeds.",
        "Leave it at 0 dB unless an input is much hotter or quieter than the other.",
        g,
        ("level", "route-level"),
    )
    yield Term(
        "total",
        "Level, total",
        "What this output adds from input to output: input level + route level + output level. Higher totals leave "
        "less room before loud peaks clip.",
        "EQ boosts and Volume come on top of this number. With two inputs at different route levels it shows a range.",
        g,
        ("headroom", "level", "route-level", "input-level"),
    )
    yield Term(
        "level-limit",
        "Level limit",
        "A safety cap this app puts on an output's level; the device itself stops at +12 dB, and Settings can only set "
        "it lower. At the limit, the card's + raises the route level instead.",
        "Change it in Settings → Channels. Lowering other outputs and raising Volume is another way to make one output "
        "relatively louder.",
        g,
        ("level", "route-level"),
    )
    yield Term(
        "dbfs",
        "dBFS",
        "Level compared with the loudest signal the DSP can carry: 0 dBFS is the maximum, so normal readings are "
        "negative. A meter at −20 dBFS has 20 dB of room left.",
        "Anything that reaches 0 dBFS clips, which sounds like distortion or a harsh crack.",
        g,
        ("meter", "clip", "headroom"),
    )
    yield Term(
        "meter",
        "Meter / peak hold",
        'The bar shows the current level; the thin line marks the highest level of the last 1.5 seconds. "No signal" '
        "means the level is below about −90 dBFS.",
        "The no-signal floor is a Settings value. (The real device's floor is not yet measured.)",
        g,
        ("dbfs", "clip"),
    )
    yield Term(
        "clip",
        "CLIP light",
        "Turns red when a channel reaches −1 dBFS and stays red until you tap it, so you can catch short overloads. If "
        "it lights, lower that output, its route, or the source.",
        "The header CLIP light lights if any channel clipped, on any page.",
        g,
        ("dbfs", "meter", "headroom"),
    )
    yield Term(
        "headroom",
        "Headroom",
        "How much louder the signal can get before it clips. With +16 dB total, peaks above about −16 dBFS at the "
        "input will overload the output.",
        "This is an estimate: it ignores EQ boosts and assumes the output clips at 0 dBFS. Watch the CLIP lights to be "
        "sure.",
        g,
        ("total", "clip", "dbfs"),
    )
    yield Term(
        "mute",
        "Mute",
        "Silences this output (or everything, with Mute all) without changing any level. Tap Undo in the message if "
        "you muted by accident.",
        "Unmuting an output that has never been sent asks first.",
        g,
        ("volume", "level"),
    )

    g = "Shaping the sound"
    yield Term(
        "eq",
        "EQ",
        "Up to 10 tone controls on each input and output, each raising or lowering one range of frequencies. Use it to "
        "tame a boomy note or lift a weak spot.",
        'Also called PEQ (parametric EQ). A band does nothing until it is switched on; "+ Add a cut" does that for '
        "you.",
        g,
        ("cut-boost", "q", "peq-type"),
    )
    yield Term(
        "cut-boost",
        "Cut / Boost",
        "Cut lowers the chosen frequencies, Boost raises them. Enter how many dB; you never need a minus sign.",
        "Small cuts (3–6 dB) usually sound better than big boosts, which also use up headroom.",
        g,
        ("eq", "q", "headroom"),
    )
    yield Term(
        "q",
        "Width (Q)",
        "How wide a cut or boost is: a higher Q touches a narrower range. At 40 Hz, Q 4 affects about 35–45 Hz and "
        "Q 0.7 about 21–77 Hz.",
        "The ranges are where the cut is at least half its depth. Wide = Q 1.4 (about 28–57 Hz at 40 Hz), Medium = "
        "Q 4, Narrow = Q 8 (about 38–43 Hz).",
        g,
        ("eq", "cut-boost"),
    )
    yield Term(
        "peq-type",
        "Filter type",
        "Peaking raises or lowers a bump around one frequency, the usual choice for a boom. Notch removes a very thin "
        "slice almost completely, and shelves raise or lower everything below (low shelf) or above (high shelf) a "
        "frequency.",
        "Low-pass and high-pass filters inside EQ work like the Band filters; band-pass keeps only a range.",
        g,
        ("eq", "peq-specialist", "band"),
    )
    yield Term(
        "peq-specialist",
        "Specialist types",
        "All-pass changes timing (phase) without changing loudness, Linkwitz Transform reshapes a sealed sub's "
        "deepest bass, Gain shifts the whole band, and Raw takes filter numbers from another tool. Most setups never "
        "need them.",
        "Use them only with a measurement (for example from REW).",
        g,
        ("peq-type", "raw", "rew"),
    )
    yield Term(
        "band",
        "Band (crossover)",
        "Limits an output to a range of frequencies: a low-pass keeps sound below its frequency, a high-pass keeps "
        'sound above. "Full range" means nothing is filtered out.',
        "Each output has two band filters, normally one high-pass and one low-pass, which together make a band such as "
        "20–80 Hz.",
        g,
        ("steepness", "filter-stages", "templates"),
    )
    yield Term(
        "steepness",
        "Steepness",
        "How fast sound fades past the band's edge: 24 dB/oct means 24 dB quieter one octave beyond it. \"Steep, "
        '24 dB/oct (LR4)" is the usual choice.',
        "LR4 means Linkwitz-Riley at 24 dB/oct; it is popular because a low-pass and a high-pass at the same frequency "
        "add back up flat. Butterworth and Bessel are alternative shapes under Advanced.",
        g,
        ("band", "filter-stages"),
    )
    yield Term(
        "filter-stages",
        "Filter stages (biquads)",
        "Each band filter has room for 4 basic stages, called biquads. A 24 dB/oct filter uses 2 and a 48 dB/oct "
        "filter uses all 4.",
        "That's why some very steep choices are greyed out: they would need more than 4 stages.",
        g,
        ("band", "steepness", "raw"),
    )
    yield Term(
        "raw",
        "Raw coefficients",
        "The five numbers (b0 b1 b2 a1 a2) the device actually runs for one filter stage; the app calculates them for "
        "you. You only need them to paste filters from a tool such as REW.",
        "A stage of all zeros passes no sound at all, so the app refuses those when reading from the device.",
        g,
        ("filter-stages", "rew", "rounding"),
    )
    yield Term(
        "templates",
        "Templates",
        'Ready-made band settings that fill in the fields for you, for example "Sub: below 80 Hz". They don\'t '
        "switch the device preset.",
        "You can change any field afterwards.",
        g,
        ("band", "device-preset"),
    )
    yield Term(
        "compressor",
        "Compressor (limiter)",
        "Turns an output down automatically when it gets louder than the threshold, to protect shakers or speakers. At "
        "a high ratio such as 50:1 it acts as a limiter.",
        "Threshold: where it starts (dBFS). Ratio: how hard it pushes back. Attack and release: how fast it reacts and "
        "lets go. The device can't report ratio, attack and release back.",
        g,
        ("dbfs", "st-cant-check"),
    )
    yield Term(
        "rounding",
        "Rounding note",
        "The device stores filters with limited precision, so its real curve can differ slightly from the planned one; "
        "the note says by how much. Under 1 dB is harmless; 1 dB or more means the real curve is noticeably off.",
        "Very low or very narrow filters are affected most.",
        g,
        ("raw", "calc-response"),
    )
    yield Term(
        "calc-response",
        "Calculated response",
        "What the app calculates the levels, filters, delays and polarity do to the signal; speakers, shakers and the "
        "room aren't included. The sum line adds outputs as if they played from one spot, so its dips may not be "
        "anything you hear or feel.",
        "For shakers the sum is especially misleading (the sound travels through the room, the vibration doesn't), so "
        "it is hidden by default for outputs marked Shakers (tactile).",
        g,
        ("mag-phase", "rounding", "tactile"),
    )
    yield Term(
        "mag-phase",
        "Magnitude / phase",
        "Magnitude is how loud each frequency comes out; phase is how far each frequency is shifted in time. Phase "
        "matters where two outputs overlap.",
        "Group delay shows the same timing as a delay in milliseconds per frequency.",
        g,
        ("calc-response", "delay", "polarity"),
    )
    yield Term(
        "fir",
        "FIR filter",
        "A custom filter loaded from a file, usually made with measurement software. Loading one makes the audio drop "
        "out briefly.",
        "Only shown when this device supports it.",
        g,
        ("rew", "st-cant-check"),
    )
    yield Term(
        "rew",
        "REW import / export",
        "Moves EQ or band filters to or from Room EQ Wizard, a free measurement program.",
        'Imported filters the app can\'t recognise show as "custom".',
        g,
        ("eq", "band", "raw"),
    )

    g = "Timing"
    yield Term(
        "delay",
        "Delay",
        "Makes an output wait so its sound arrives together with a farther speaker's; 1 ms is about 34 cm (13.5 in) of "
        "sound travel. The device allows up to 79.8 ms.",
        "Delay the closer one. Shakers in your seat are 0 m away, so they usually wait for the sub.",
        g,
        ("samples", "line-up", "polarity"),
    )
    yield Term(
        "samples",
        "Samples",
        "The device counts delay in steps of 1/96,000 of a second (about 0.01 ms). 1 ms is 96 samples.",
        "The app rounds your value to the nearest sample.",
        g,
        ("delay",),
    )
    yield Term(
        "line-up",
        "Line up with…",
        "Works out the delay from distances: tell it how much closer this output is than another, and it says how long "
        "to wait.",
        "It assumes sound travels 343 m/s; amplifiers and your body add a little lag, so fine-tune by feel.",
        g,
        ("delay", "tactile"),
    )
    yield Term(
        "polarity",
        "Polarity",
        "180° flips the signal's push and pull; 0° is normal. If bass gets thinner when two outputs play together, try "
        "flipping one.",
        "It adds no delay and is harmless to try.",
        g,
        ("delay", "mag-phase"),
    )

    g = "Routing and inputs"
    yield Term(
        "routing",
        "Routing",
        "Chooses which inputs feed each output and at what level. An output with no route plays nothing.",
        "Each box is one input → output path with its own route level.",
        g,
        ("route-level", "mono-sum", "source"),
    )
    yield Term(
        "mono-sum",
        "Mono sum",
        "Sends both inputs to one output at −6 dB each, so a stereo source plays in full on one sub or shaker without "
        "overloading.",
        "The preview lists exactly which routes change.",
        g,
        ("routing", "route-level"),
    )
    yield Term(
        "source",
        "Source",
        "Which input connector the miniDSP listens to: Analog, Toslink (optical) or USB. The wrong source means "
        "silence on every output.",
        "",
        g,
        ("routing",),
    )
    yield Term(
        "not-in-use",
        "Not in use",
        "Marks a channel you don't use, so the app stops pointing out that it's muted or unrouted. It folds the "
        "channel away on Home.",
        "Set it in Settings → Channels or from the note itself. It changes nothing on the device.",
        g,
        ("b-not-in-use", "tactile"),
    )
    yield Term(
        "tactile",
        "Shakers (tactile)",
        "Tells the app this output drives shakers, so graphs open at 5–300 Hz, the misleading sum line is hidden, and "
        "Line up picks it.",
        "It changes nothing on the device.",
        g,
        ("calc-response", "line-up", "not-in-use"),
    )

    g = "Presets and profiles"
    yield Term(
        "device-preset",
        "Device preset",
        "One of 4 complete setups stored inside the miniDSP; ▶ marks the one playing. Switching makes the device "
        "reload, so sound can drop for a few seconds.",
        "The remote and other apps can switch presets too; the header follows.",
        g,
        ("editing-preset", "waiting", "profile"),
    )
    yield Term(
        "editing-preset",
        "Editing preset",
        "Which preset your changes go to. If it isn't the one playing, changes wait and are sent when it plays.",
        "The amber band reminds you.",
        g,
        ("device-preset", "waiting"),
    )
    yield Term(
        "waiting",
        "Waiting for preset N (⏸)",
        "A change saved for a preset that isn't playing. It is sent automatically when that preset starts playing.",
        '"What\'s waiting?" lists them by name.',
        g,
        ("editing-preset", "st-waiting"),
    )
    yield Term(
        "profile",
        "Profile",
        "A named copy of a whole preset, saved in this app (not on the device). Load it into any of the 4 presets to "
        "reuse or restore a setup.",
        "Profiles survive a device reset and can be exported as a file.",
        g,
        ("device-preset", "store-vs-play", "same-as", "history"),
    )
    yield Term(
        "same-as",
        "Same as preset N",
        "This profile is exactly what preset N holds right now.",
        "",
        g,
        ("profile",),
    )
    yield Term(
        "store-vs-play",
        "Store / Store and play now",
        "Store puts the profile into the preset and sends it when that preset plays. Store and play now switches to it "
        "straight away (sound drops for a few seconds).",
        "",
        g,
        ("profile", "waiting", "device-preset"),
    )
    yield Term(
        "history",
        "History",
        "Every change to a preset is kept as a numbered step, and you can go back to any step.",
        "Going back creates a new step; history is never rewritten.",
        g,
        ("profile",),
    )

    g = "Sync and the device"
    yield Term(
        "status",
        "Status",
        'Whether the device holds what this app sent. "✓ All sent" means every setting was sent and accepted; it '
        "doesn't mean the device was checked.",
        "See each status below. The header shows the most important one.",
        g,
        ("st-sent", "st-sending", "st-waiting", "st-failed", "st-unknown", "st-changed", "st-cant-check"),
    )
    yield Term(
        "st-sent",
        "Sent",
        "The app sent this value and the device accepted it. Sent settings show no mark.",
        "",
        g,
        ("status", "last-sent"),
    )
    yield Term(
        "st-sending",
        "Sending ⟳",
        "Being sent to the device right now; it usually takes under a second. Big changes (a preset switch or profile) "
        "count down.",
        "If it stays, the device may be busy or disconnected.",
        g,
        ("status",),
    )
    yield Term(
        "st-waiting",
        "Waiting for preset N ⏸",
        "Saved for a preset that isn't playing; sent when it plays.",
        "",
        g,
        ("status", "waiting"),
    )
    yield Term(
        "st-failed",
        "Failed ⚠",
        "The last send didn't reach the device. Your value is kept; tap Retry.",
        "",
        g,
        ("status", "send"),
    )
    yield Term(
        "st-unknown",
        "Unknown ?",
        "The app doesn't know what the device holds here: it hasn't sent or read it since setup. It is sent the next "
        "time you change it, or with Send all.",
        "",
        g,
        ("status", "set-up", "check-device"),
    )
    yield Term(
        "st-changed",
        "Changed on device ≠",
        "Check device found a different value on the device than the app sent, for example after another app or the "
        "remote changed it. Choose which one to keep.",
        '"Send the app\'s value" overwrites the device; "Use the device\'s values" updates the app.',
        g,
        ("check-device", "use-device", "forget"),
    )
    yield Term(
        "st-cant-check",
        "Can't check ⊘",
        "The device can't report this setting back (routing, on/off switches, compressor timing, FIR), so the app "
        "trusts what it sent. That isn't an error.",
        "",
        g,
        ("status", "check-device"),
    )
    yield Term(
        "last-sent",
        "Last sent",
        "The value the app last sent and the device accepted. It isn't a reading from the device.",
        "",
        g,
        ("st-sent", "check-device"),
    )
    yield Term(
        "check-device",
        "Check device",
        "Reads the playing preset back from the miniDSP and compares it with what the app sent. Nothing is written.",
        "Only the playing preset can be read.",
        g,
        ("st-changed", "st-cant-check", "use-device", "forget"),
    )
    yield Term(
        "use-device",
        "Use the device's values…",
        "Makes the app's values equal to what the device reported, for the listed settings. Nothing is sent to the "
        "device, and you can undo it for 30 seconds.",
        "Values that look like read errors (all-zero filters) are never used.",
        g,
        ("check-device", "forget"),
    )
    yield Term(
        "forget",
        "Forget these values",
        "Marks the listed settings Unknown, so the app stops assuming it knows them. Nothing changes on the device.",
        "They are sent next time you change them or use Send all.",
        g,
        ("st-unknown", "check-device"),
    )
    yield Term(
        "send",
        "Send / Send all",
        "Writes the app's values to the device, replacing what it holds. Send all writes every setting of the preset, "
        "with outputs muted while it runs if that's on in Settings.",
        "",
        g,
        ("status", "st-failed"),
    )
    yield Term(
        "set-up",
        "Set up this device",
        "The one-time step that tells the app what your miniDSP already holds, so later changes act on the right "
        "values. Nothing is written unless you choose to.",
        "",
        g,
        ("remember", "overwrite", "dont-know"),
    )
    yield Term(
        "remember",
        "Remember (trust)",
        "The app takes your script, backup or reading as what the device holds. Nothing is written.",
        "Choosing factory defaults this way needs an extra confirmation, because it's almost never true for a device "
        "that was set up before.",
        g,
        ("set-up", "overwrite", "dont-know"),
    )
    yield Term(
        "overwrite",
        "Overwrite the device (push)",
        "Sends the app's whole setup to the preset, replacing what the device holds. Outputs are muted while it runs.",
        "",
        g,
        ("set-up", "remember", "dont-know"),
    )
    yield Term(
        "dont-know",
        "Don't know yet (unknown)",
        "The app assumes nothing and sends each setting the first time you change it.",
        "Check device fills in whatever the device can report.",
        g,
        ("set-up", "st-unknown", "check-device"),
    )

    g = "Health badges"
    yield Term(
        "b-ready",
        "✓ Ready",
        "Routed, unmuted, lets sound through, and all settings sent. The app can't hear, so check the meter to see "
        "sound actually flowing.",
        "",
        g,
        ("meter",),
    )
    yield Term(
        "b-silent",
        "⛔ Silent",
        "Something in the app's settings stops all sound on this output; the line underneath says what, with a fix.",
        "",
        g,
        ("routing", "mute"),
    )
    yield Term("b-muted", "🔇 Muted", "This output is muted. Its level is kept for when you unmute.", "", g, ("mute",))
    yield Term(
        "b-not-in-use",
        "◌ Not in use",
        "You marked this channel as unused in Settings, so the app doesn't point out its notes.",
        "",
        g,
        ("not-in-use",),
    )
    yield Term(
        "b-check",
        "⚠ Check",
        "Worth a look, but not silent: for example a high total level, or settings that didn't reach the device.",
        "",
        g,
        ("total", "status"),
    )
    yield Term(
        "b-overload",
        "⚠ Overload",
        "This output clipped. Lower it, its route or the source; tap CLIP to reset.",
        "",
        g,
        ("clip", "headroom"),
    )


TERMS: dict[str, Term] = {t.id: t for t in _terms()}


def term(term_id: str) -> Term:
    """The term with this id; a KeyError names the missing id (templates must only use ids that exist)."""
    try:
        return TERMS[term_id]
    except KeyError:
        raise KeyError(f"unknown glossary term {term_id!r}") from None


def grouped() -> list[tuple[str, list[Term]]]:
    """The terms by group, in the order of ``GROUPS`` (for /help)."""
    return [(name, [t for t in TERMS.values() if t.group == name]) for name in GROUPS]


# Ids only need to be unique within one page; a process-wide counter guarantees that without per-render state.
_ids = itertools.count(1)


def help_button(term_id: str, root: str = "", key: str | None = None) -> Markup:
    """The "?" toggletip for one term (design §13.1): a disclosure button and its non-modal popover.

    Only spans inside, so it is valid anywhere phrasing content is (headings, table headers, next to a control). Never
    put it inside a ``<label>`` (a button there is invalid and could become the label's control). Inside an Alpine
    ``x-for`` template pass ``key``, a JavaScript expression unique per copy (``"group.index"``): the ids are then
    bound by Alpine, so every copy gets its own. static/js/help.js opens it on click, tap, Enter or Space; never on
    hover.
    """
    t = term(term_id)
    uid = f"tt-{t.id}-{next(_ids)}"
    href = f"{root}/help#{t.id}"
    if key is None:
        controls = f'aria-controls="{uid}"'
        pop_id = f'id="{uid}" aria-labelledby="{uid}-t"'
        title_id = f'id="{uid}-t"'
    else:
        expr = escape(f"'{uid}-' + ({key})")
        controls = f':aria-controls="{expr}"'
        pop_id = f':id="{expr}" :aria-labelledby="{expr} + \'-t\'"'
        title_id = f":id=\"{expr} + '-t'\""
    return Markup(
        '<span class="tt">'
        f'<button type="button" class="tt-btn" data-tt-btn aria-expanded="false" {controls}'
        f' aria-label="What is {escape(t.label)}?"><span class="tt-glyph" aria-hidden="true">?</span></button>'
        f'<span {pop_id} class="tt-pop" role="dialog" data-tt-pop hidden>'
        f'<strong class="tt-title" {title_id}>{escape(t.label)}</strong>'
        f'<span class="tt-text">{escape(t.short)}</span>'
        f'<a class="tt-more" href="{escape(href)}">More <span aria-hidden="true">→</span>'
        f'<span class="visually-hidden"> about {escape(t.label)}</span></a>'
        "</span></span>"
    )
