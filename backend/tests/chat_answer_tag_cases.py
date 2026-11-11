"""Shared parser and stream cases for answer section boundaries."""

TAG_CASES = [
    ("[c1.1] Moved.", "", ""),
    ("Intro\n\n[c18.1] Answer", "Intro", "Intro"),
    ("Two answers:\n\n[c1.1][c1.2] shared\n\n[c1.3] final", "Two answers:", "Two answers:"),
    ("Intro\r\n \r\n  [c2.3] Answer", "Intro", "Intro"),
    ("Intro\n\n\n[c1.1] Answer", "Intro\n", "Intro\n"),
    ("Intro\n\n[c1.1]", "Intro", "Intro"),
    ("[c", None, ""),
    ("Intro\n\n[", None, "Intro"),
    ("Intro\n\n[c18", None, "Intro"),
    ("Intro\n\n[c18.", None, "Intro"),
    ("Intro\n\n[c18.1", None, "Intro"),
    ("Intro\n\n", None, "Intro"),
    ("See [c1.1] inline, not a section", None, "See [c1.1] inline, not a section"),
    ("Intro\n[c1.1] Answer", None, "Intro\n[c1.1] Answer"),
    ("Intro\n\n[code] sample", None, "Intro\n\n[code] sample"),
    ("Intro\n\n[c] sample", None, "Intro\n\n[c] sample"),
    ("Intro\n\n[c0.1] sample", None, "Intro\n\n[c0.1] sample"),
    ("Intro\n\n[c1 is not a tag", None, "Intro\n\n[c1 is not a tag"),
    ("No tags here.", None, "No tags here."),
]
