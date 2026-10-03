"""Create a workspace and its first checkpoint (legacy entry point)."""
import argparse
from workspace_cli import main

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="./workspace")
    args = parser.parse_args()
    raise SystemExit(main(["--workspace", args.workspace, "init"]))
