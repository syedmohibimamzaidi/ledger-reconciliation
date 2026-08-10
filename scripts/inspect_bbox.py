"""One-shot: what shape are the bboxes actually in?"""
from datasets import load_dataset

ds = load_dataset("jsdnrs/ICDAR2019-SROIE", split="train")
row = ds.remove_columns(["image"])[0]

words, boxes = row["words"], row["bboxes"]
print(f"words: {len(words)}  bboxes: {len(boxes)}")
print(f"first bbox: {boxes[0]}  (len {len(boxes[0])})")
print(f"image_size: {row.get('image_size')}\n")
for w, b in list(zip(words, boxes))[:8]:
    print(f"  {str(b):<44} {w!r}")