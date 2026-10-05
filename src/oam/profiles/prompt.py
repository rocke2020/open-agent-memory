"""Load the generation rules shared by Profile publication and refresh identity."""

from importlib.resources import files

PROFILE_SYSTEM = (
    files("oam.profiles").joinpath("prompt.txt").read_text(encoding="utf-8").rstrip("\n")
)
