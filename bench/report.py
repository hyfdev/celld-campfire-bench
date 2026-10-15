import argparse
import pathlib

from aggregate import load
from render import readme_results, render

ROOT = pathlib.Path(__file__).resolve().parents[1]
RECORDED = ROOT / "results/2026-10-15"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?", type=pathlib.Path, default=RECORDED)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    directory = args.directory.resolve()
    raw, cold, validation, summary = load(directory)
    report = render(raw, cold, summary)
    if args.check:
        assert (directory / "REPORT.md").read_text() == report, "REPORT.md differs from the data"
        if directory == RECORDED:
            readme = (ROOT / "README.md").read_text()
            block = readme.split("<!-- benchmark:start -->\n", 1)[1].split("\n<!-- benchmark:end -->", 1)[0]
            assert block == readme_results(summary), "README tables differ from the data"
        print(f"Verified source snapshots and report tables: {len(raw['trials'])} pressure trials, {len(cold['trials'])} startup trials.")
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
