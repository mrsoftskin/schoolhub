"""Enable `python -m brain ...` - the launcher and installer call the CLI this
way (through the SIGNED interpreter) so nothing depends on the unsigned
`brain.exe` console-script that Smart App Control could block."""

from .cli import main

if __name__ == "__main__":
    main()
