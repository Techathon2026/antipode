"""只测试新增的30个标题，不更新模型、数据或固定划分。"""

import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer


ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "runs" / "20261016_215321_020467"
MODEL_DIR = RUN / "best_model"
DATA = ROOT / "data" / "email_headers_20261016_T01-T08_labeled.csv"


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalized(text):
    text = re.sub(r"\\r\\n|\\n|\\r", " ", text)
    text = unicodedata.normalize("NFKC", text).lower().strip()
    text = re.sub(r"^(?:(?:re|fw|fwd|回复|答复|转发)\s*:\s*)+", "", text)
    return re.sub(r"\s+", "", text)


def accuracy(rows):
    correct = sum(row["correct"] for row in rows)
    return {"count": len(rows), "correct": correct,
            "accuracy": correct / len(rows) if rows else None}


def main():
    protected = [DATA, *MODEL_DIR.glob("*"), * (ROOT / "training").glob("*.py"),
                 *(ROOT / "data" / "splits").glob("*")]
    hashes = {str(p): digest(p) for p in protected if p.is_file()}
    with DATA.open(encoding="utf-8-sig", newline="") as stream:
        added = [row for row in csv.DictReader(stream) if row["id"].startswith("import:emails:")]
    assert len(added) == 30, f"Expected 30 newly added rows, got {len(added)}"
    added_keys = {normalized(row["subject"]) for row in added}
    for split in ("train", "val", "test"):
        with (ROOT / "data" / "splits" / f"{split}.csv").open(encoding="utf-8-sig", newline="") as stream:
            overlap = added_keys & {normalized(row["subject"]) for row in csv.DictReader(stream)}
        assert not overlap, f"Title overlap with {split} split"

    settings = json.loads((RUN / "summary.json").read_text(encoding="utf-8"))
    tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR, local_files_only=True, fix_mistral_regex=False)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_DIR, local_files_only=True, use_safetensors=True,
    )
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device).eval()
    predictions = []
    with torch.inference_mode():
        for start in range(0, len(added), 8):
            rows = added[start:start + 8]
            inputs = tokenizer([row["subject"] for row in rows], truncation=True,
                               padding="max_length", max_length=128,
                               return_token_type_ids=False, return_tensors="pt")
            inputs = {key: value.to(device) for key, value in inputs.items()}
            probs = model(**inputs).logits.softmax(dim=-1).cpu()
            for row, scores in zip(rows, probs):
                index = scores.argmax().item()
                label = model.config.id2label[index]
                predictions.append({
                    "id": row["id"], "subject": row["subject"], "reference_label": row["label"],
                    "predicted_label": label, "confidence": scores[index].item(),
                    "correct": label == row["label"], "review_status": row["review_status"],
                    "annotation_note": row["annotation_note"],
                    "probabilities": {model.config.id2label[i]: value for i, value in enumerate(scores.tolist())},
                })
    certain = [row for row in predictions if row["review_status"] != "需重点复核"]
    uncertain = [row for row in predictions if row["review_status"] == "需重点复核"]
    counts = Counter(row["reference_label"] for row in predictions)
    summary = {
        "model": str(MODEL_DIR), "best_epoch": settings["best_epoch"], "device": str(device),
        "all_provisional_labels": accuracy(predictions), "excluding_flagged_labels": accuracy(certain),
        "flagged_labels_only": accuracy(uncertain),
        "reference_class_counts": dict(sorted(counts.items())),
        "by_reference_class": {label: accuracy([row for row in predictions if row["reference_label"] == label]) for label in sorted(counts)},
        "no_normalized_title_overlap_with_existing_splits": True,
        "limitations": ["Reference labels are AI preannotations, not independently human-verified ground truth.",
                        "Only three reference classes are represented; this is not a full eight-class benchmark.",
                        "Similar events and templates may exist in the training set despite no matching normalized title."],
    }
    for name, expected in hashes.items():
        assert digest(Path(name)) == expected, f"Unexpected modification: {name}"
    summary["existing_model_data_programs_and_splits_unchanged"] = True
    output = ROOT / "annotation_work" / ("new30_test_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    output.mkdir(exist_ok=False)
    (output / "predictions.json").write_text(json.dumps({"summary": summary, "predictions": predictions}, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 新增30条邮件模型测试", "", f"模型：{MODEL_DIR}（最佳第{settings['best_epoch']}轮）", "",
             "对照标签为AI预标注，6条需重点复核；本次未训练或修改模型。", "",
             "| 范围 | 分对/总数 | 准确率 |", "|---|---:|---:|"]
    for title, rows in (("全部预标注样本", predictions), ("排除待重点复核样本", certain), ("待重点复核样本", uncertain)):
        metrics = accuracy(rows)
        lines.append(f"| {title} | {metrics['correct']}/{metrics['count']} | {metrics['accuracy']:.2%} |")
    lines += ["", "仅覆盖T01、T06、T07。新增标题与原三个划分无规范化标题重合，但可能有相似模板或同类事件。", "",
              "| 标题 | 对照标签 | 模型预测 | 预测概率 | 状态 |", "|---|---|---|---:|---|"]
    for row in predictions:
        subject = row["subject"].replace("|", "\\|").replace("\n", " ")
        status = "一致" if row["correct"] else "不一致"
        if row["review_status"] == "需重点复核":
            status += "；标签待复核"
        lines.append(f"| {subject} | {row['reference_label']} | {row['predicted_label']} | {row['confidence']:.2%} | {status} |")
    (output / "测试报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("REPORT:", output / "测试报告.md")
    print("不一致记录：")
    for row in predictions:
        if not row["correct"]:
            print(json.dumps({key: row[key] for key in ("subject", "reference_label", "predicted_label", "confidence", "review_status")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
