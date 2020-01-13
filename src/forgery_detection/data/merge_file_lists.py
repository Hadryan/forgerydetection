import os
from pathlib import Path

import numpy as np

from forgery_detection.data.face_forensics.splits import TEST_NAME
from forgery_detection.data.face_forensics.splits import TRAIN_NAME
from forgery_detection.data.face_forensics.splits import VAL_NAME
from forgery_detection.data.set import FileList

resampled_file_list = FileList.load(
    "/data/ssd1/file_lists/c40/tracked_resampled_faces.json"
)
detection_file_list = FileList.load(
    "/data/ssd1/file_lists/c40/detection_challenge_112.json"
)


resampled_root = Path(resampled_file_list.root)
detection_root = Path(detection_file_list.root)
common_path = os.path.commonpath([detection_root, resampled_root])
resampled_relative_to_root = os.path.relpath(resampled_root, common_path)
detection_relative_to_root = os.path.relpath(detection_root, common_path)

print(common_path, resampled_relative_to_root, detection_relative_to_root)

# change class idx values for resampled file list
# make youtube one value higher
resampled_file_list.class_to_idx["DeepFakeDetection"] = 4
resampled_file_list.classes[4] = "DeepFakeDetection"
resampled_file_list.class_to_idx["youtube"] = 5
resampled_file_list.classes.append("youtube")

resampled_file_list.root = common_path

for split in resampled_file_list.samples.values():
    for item in split:
        if item[1] == 4:
            item[1] = 5

        item[0] = resampled_relative_to_root + "/" + item[0]

#
print(resampled_file_list.samples["train"][-1])

# change class idx values for detection file list
detection_file_list.class_to_idx = resampled_file_list.class_to_idx
detection_file_list.classes = resampled_file_list.classes
for split in detection_file_list.samples.values():
    for item in split:
        if item[1] == 0:
            item[1] = 5
        elif item[1] == 1:
            item[1] = 4
        item[0] = detection_relative_to_root + "/" + item[0]

print(detection_file_list.samples["train"][-1])
print(
    resampled_file_list.samples_idx["train"][-1],
    len(resampled_file_list.samples["train"]),
)

# actually merge the samples

for split_name in [TRAIN_NAME, VAL_NAME, TEST_NAME]:
    resampled_split = resampled_file_list.samples[split_name]
    resampled_split_len = len(resampled_split)

    detection_split = detection_file_list.samples[split_name]
    resampled_split.extend(detection_split)

    detection_idx = detection_file_list.samples_idx[split_name]
    detection_idx = (np.array(detection_idx) + resampled_split_len).tolist()

    resampled_idx = resampled_file_list.samples_idx[split_name]
    resampled_idx.extend(detection_idx)

#
print(detection_file_list.samples["train"][-1])
print(
    resampled_file_list.samples_idx["train"][-1],
    len(resampled_file_list.samples["train"]),
)

# save merged file_list

resampled_file_list.save("/data/ssd1/file_lists/c40/resampled_and_detection_112.json")

merged = FileList.load("/data/ssd1/file_lists/c40/resampled_and_detection_112.json")
d = merged.get_dataset("train")