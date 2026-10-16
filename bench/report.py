import argparse
import pathlib

from aggregate import load, load_compile_cache
from render import compile_cache_results, readme_results, render

ROOT = pathlib.Path(__file__).resolve().parents[1]
RECORDED = ROOT / "results/2026-10-16"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?", type=pathlib.Path, default=RECORDED)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    directory = args.directory.resolve()
    if (directory / "cold-cache-results.json").is_file():
        compilation = load_compile_cache(directory)
        report = "# Wasmtime compilation-cache startup comparison\n\n" + compile_cache_results(compilation, "cold-cache-results.json") + "\n"
        if args.check:
            assert (directory / "REPORT.md").read_text() == report, "REPORT.md differs from the data"
            print(f"Verified source snapshots, cache hits, and {len(compilation[0]['trials'])} startup trials.")
        else:
            print(report, end="")
        return
    raw, cold, validation, summary = load(directory)
    compilation = load_compile_cache(directory / "compile-cache")
    report = render(raw, cold, summary, compilation)
    if args.check:
        assert (directory / "REPORT.md").read_text() == report, "REPORT.md differs from the data"
        if directory == RECORDED:
            readme = (ROOT / "README.md").read_text()
            block = readme.split("<!-- benchmark:start -->\n", 1)[1].split("\n<!-- benchmark:end -->", 1)[0]
            assert block == readme_results(summary, compilation), "README tables differ from the data"
        extra = f", {len(compilation[0]['trials'])} compilation-cache startup trials" if compilation else ""
        print(f"Verified source snapshots and report tables: {len(raw['trials'])} pressure trials, {len(cold['trials'])} startup trials{extra}.")
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
