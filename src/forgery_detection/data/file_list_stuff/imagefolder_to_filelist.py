from pathlib import Path

import numpy as np

from forgery_detection.data.file_lists import FileList

root_dir = Path("/mnt/ssd2/sebastian/set/avspeech_moria_112")

f = FileList(root_dir, ["avspeech"], 8)

#
# images = list((root_dir / "train19k").glob("*.jpg"))
# f.add_data_points(images, "celeba", "train", np.arange(0, len(images)))
#
# images = list((root_dir / "val").glob("*.jpg"))
# f.add_data_points(images, "celeba", "train", np.arange(0, len(images)))
#
# images = list((root_dir / "test").glob("*.jpg"))
# f.add_data_points(images, "celeba", "val", np.arange(0, len(images)))
#
# images = list((root_dir / "test").glob("*.jpg"))
# f.add_data_points(images, "celeba", "test", np.arange(0, len(images)))

videos = sorted(root_dir.iterdir())
train = videos[: int(len(videos) * 0.9)]
val = videos[int(len(videos) * 0.9) :]

samples_per_video = 100

for label in train:
    images = sorted(label.glob("*.png"))
    f.add_data_points(
        images,
        "avspeech",
        "train",
        np.rint(
            np.linspace(7, len(images), min(samples_per_video, len(images))) - 1
        ).astype(int),
    )

for label in val:
    images = sorted(label.glob("*.png"))
    f.add_data_points(
        images,
        "avspeech",
        "val",
        np.rint(
            np.linspace(7, len(images), min(samples_per_video, len(images))) - 1
        ).astype(int),
    )

f.root = str(f.root)
f.save("/data/ssd1/file_lists/fid/moria_100.json")

print(f.get_dataset("train"))
print(f.get_dataset("val"))
print(f.get_dataset("test"))
