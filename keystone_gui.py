"""Launch revon from the former desktop entry point."""
def main():
    from revon_gui import main as launch
    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
