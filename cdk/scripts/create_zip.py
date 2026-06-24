"""
Create a zip file from the agent bundle directory.
Used during CDK bundling to package agent code + dependencies.
"""
import zipfile
import os
import sys
import tempfile
from pathlib import Path


def main():
    bundle_dir = Path(os.environ.get("BUNDLE_DIR", tempfile.gettempdir() + "/agent-bundle"))
    output_zip = Path(os.environ.get("OUTPUT_ZIP", "/asset-output/agent-code.zip"))

    if not bundle_dir.exists():
        print(f"Error: Bundle directory does not exist: {bundle_dir}", file=sys.stderr)
        return 1

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    ignore_dirs = {"__pycache__", ".git", ".venv", "node_modules", ".bedrock_agentcore"}

    try:
        file_count = 0
        with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(bundle_dir):
                dirs[:] = [d for d in dirs if d not in ignore_dirs]
                rel_root = os.path.relpath(root, bundle_dir)
                if rel_root == ".":
                    rel_root = ""
                for file in files:
                    file_path = Path(root) / file
                    file_rel = os.path.join(rel_root, file) if rel_root else file
                    zipf.write(file_path, file_rel)
                    file_count += 1
        print(f"Created zip with {file_count} files")
        return 0
    except Exception as e:
        print(f"Error creating zip file: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
