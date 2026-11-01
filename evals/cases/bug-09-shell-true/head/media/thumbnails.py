import subprocess

from media.storage import upload_path


def make_thumbnail(uploaded_name: str) -> str:
    """Create a 200px thumbnail next to an uploaded image. `uploaded_name` is the file name
    the user gave when uploading."""
    source = upload_path(uploaded_name)
    target = source.with_name(f"thumb_{source.name}")
    subprocess.run(f"convert {source} -resize 200x200 {target}", shell=True, check=True)
    return str(target)
