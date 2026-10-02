#!/usr/bin/env python3
"""Single entry point for the standalone sonyctl binary.

  sonyctl                 -> opens the window
  sonyctl gui [--demo]    -> opens the window (with GUI options)
  sonyctl <command> ...   -> command-line tool (battery, anc, doctor, ...)
"""
import sys


def main():
    args = sys.argv[1:]
    if not args or args[0] == "gui":
        import sonyctl_gui
        sys.argv = [sys.argv[0]] + args[1:]
        sonyctl_gui.main()
        return 0
    import sonyctl
    return sonyctl.main(args)


if __name__ == "__main__":
    sys.exit(main())
