"""Read authorized mailbox headers and export a CSV for manual labeling.

Uses only the Python standard library. No email bodies or attachments are fetched.
"""

import argparse
import base64
import csv
import getpass
import imaplib
import os
import re
import shutil
import socket
import ssl
import sys
import tempfile
from datetime import datetime
from email import policy
from email.parser import BytesHeaderParser
from pathlib import Path


HEADER_QUERY = "(UID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID REFERENCES IN-REPLY-TO)])"
FIELDS = ["id", "subject", "sender", "date", "label", "folder", "needs_body", "message_id", "group_id"]
BATCH_SIZE = 100


def spreadsheet_text(value):
    """Keep untrusted email headers from being interpreted as spreadsheet formulas."""
    text = " ".join(str(value).split())
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def parse_header_response(response, uidvalidity):
    """Parse literal header payloads, matching each message to its server UID."""
    parser = BytesHeaderParser(policy=policy.default)
    for item in response:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        metadata, payload = item
        match = re.search(rb"\bUID\s+(\d+)\b", metadata)
        if match is None or not isinstance(payload, bytes):
            raise ValueError("服务器返回的邮件头缺少 UID 或内容，已停止导出。")
        message = parser.parsebytes(payload)
        uid = match.group(1).decode("ascii")
        message_id = str(message.get("Message-ID", "")).strip()
        references = str(message.get("References", "")) or str(message.get("In-Reply-To", ""))
        thread_ids = re.findall(r"<[^<>]+>", references)
        yield {
            "id": f"{uidvalidity}:{uid}",
            "subject": str(message.get("Subject", "")),
            "sender": str(message.get("From", "")),
            "date": str(message.get("Date", "")),
            "label": "",
            "needs_body": "",
            "message_id": message_id,
            "group_id": thread_ids[0] if thread_ids else message_id or f"{uidvalidity}:{uid}",
        }


def folder_display_name(raw_name):
    """Decode the modified UTF-7 mailbox names used by IMAP servers."""
    name = raw_name.decode("ascii")
    if name.startswith('"') and name.endswith('"'):
        name = re.sub(r"\\(.)", r"\1", name[1:-1])

    def decode_segment(match):
        value = match.group(1)
        if not value:
            return "&"
        value = value.replace(",", "/")
        return base64.b64decode(value + "=" * (-len(value) % 4)).decode("utf-16-be")

    return re.sub(r"&([^-]*)-", decode_segment, name)


def list_folders(client, inbox_only=False):
    if inbox_only:
        return [(b"INBOX", "INBOX")]
    status, response = client.list()
    if status != "OK":
        raise RuntimeError("无法列出邮箱文件夹。可以加 --inbox-only 仅导出收件箱。")
    folders = []
    for item in response or []:
        if item is None:
            continue
        if not isinstance(item, bytes):
            raise RuntimeError("服务器返回了暂不支持的文件夹格式。可以加 --inbox-only。")
        match = re.match(rb'^\((.*?)\)\s+(?:"(?:\\.|[^"])*"|NIL)\s+(.+)$', item)
        if match is None:
            raise RuntimeError("无法解析邮箱文件夹列表。可以加 --inbox-only。")
        flags, raw_name = match.groups()
        if b"\\noselect" in flags.lower().split():
            continue
        folders.append((raw_name, folder_display_name(raw_name)))
    return folders


def collect_folder_headers(client, raw_name, folder_name, limit=None):
    # EXAMINE (readonly=True) and BODY.PEEK preserve mailbox state and unread flags.
    status, _ = client.select(raw_name, readonly=True)
    if status != "OK":
        raise RuntimeError(f"无法以只读方式打开文件夹：{folder_name}；未生成不完整表格。")
    _, validity_data = client.response("UIDVALIDITY")
    validity = (validity_data[0] if validity_data else None)
    if not validity:
        raise RuntimeError("服务器未返回 UIDVALIDITY，无法生成稳定邮件编号。")
    validity = validity.decode("ascii")
    status, result = client.uid("search", None, "ALL")
    if status != "OK":
        raise RuntimeError(f"无法查询文件夹邮件列表：{folder_name}")
    uids = sorted(result[0].split(), key=int) if result and result[0] else []
    print(f"文件夹 {folder_name} 共有 {len(uids)} 封邮件。")
    uids.reverse()
    if limit is not None:
        uids = uids[:limit]

    rows = []
    for start in range(0, len(uids), BATCH_SIZE):
        batch = uids[start:start + BATCH_SIZE]
        status, response = client.uid("fetch", b",".join(batch), HEADER_QUERY)
        if status != "OK":
            raise RuntimeError("读取邮件头失败；请稍后重新运行。")
        parsed = list(parse_header_response(response, validity))
        if len(parsed) != len(batch):
            print("提示：部分邮件在采集期间已移走，或服务器未返回对应邮件头。")
        by_uid = {row["id"].rsplit(":", 1)[1]: row for row in parsed}
        for uid in batch:
            row = by_uid.get(uid.decode("ascii"))
            if row is None:
                continue
            row["id"] = f"{folder_name}:{row['id']}"
            row["folder"] = folder_name
            rows.append(row)
        print(f"本文件夹已提取 {len(rows)} / {len(uids)} 封邮件头。")
    return rows


def collect_headers(client, limit=None, inbox_only=False):
    folders = list_folders(client, inbox_only)
    print(f"将读取 {len(folders)} 个可选择文件夹；默认无数量上限。")
    rows = []
    for raw_name, folder_name in folders:
        remaining = None if limit is None else limit - len(rows)
        if remaining is not None and remaining <= 0:
            break
        rows.extend(collect_folder_headers(client, raw_name, folder_name, remaining))
    return rows


def choose_output(value=None):
    interactive = value is None
    if interactive:
        value = input("保存方式：输入 new 新建，或输入已有 CSV 文件路径：").strip()
    if value.lower() == "new":
        return Path("data") / f"email_headers_{datetime.now():%Y%m%d_%H%M%S_%f}.csv", False
    output = Path(value.strip('"')).expanduser()
    if output.suffix.lower() != ".csv":
        raise ValueError("请输入 .csv 文件路径，或输入 new。")
    if not output.is_file():
        if interactive:
            raise ValueError("CSV 文件不存在，请检查路径；新建请输入 new。")
        return output, False
    return output, True


def email_key(row):
    """Message-ID works across folders/accounts; UID alone does not."""
    if row.get("message_id", "").strip():
        return ("message_id", row["message_id"].strip())
    return ("header", *(row.get(key, "") for key in ("id", "subject", "sender", "date", "folder")))


def save_csv(rows, output, append=False):
    output.parent.mkdir(parents=True, exist_ok=True)
    if not append:
        with output.open("x", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows({key: spreadsheet_text(value) for key, value in row.items()} for row in rows)
        return len(rows)

    # Read at save time so labels edited during collection are retained too.
    before = output.stat()
    with output.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        required = {"id", "subject", "sender", "date", "label", "message_id"}
        if not fields or len(fields) != len(set(fields)) or not required.issubset(fields):
            raise ValueError("原 CSV 列名不兼容：需要 id、subject、sender、date、label、message_id。")
        known = set()
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("原 CSV 存在列数不匹配的记录，未修改文件。")
            known.add(email_key(row))

    additions = []
    for row in rows:
        clean = {key: spreadsheet_text(value) for key, value in row.items()}
        key = email_key(clean)
        if key not in known:
            known.add(key)
            additions.append(clean)
    if not additions:
        return 0

    # Copy old bytes unchanged, append in a sibling file, then replace atomically.
    # Extra user columns are retained and left blank on newly collected rows.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".tmp", delete=False) as target:
            temp_path = Path(target.name)
            with output.open("rb") as source:
                shutil.copyfileobj(source, target)
                source.seek(-1, os.SEEK_END)
                if source.read(1) not in (b"\n", b"\r"):
                    target.write(b"\r\n")
        with temp_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writerows(additions)
            handle.flush()
            os.fsync(handle.fileno())
        current = output.stat()
        if (current.st_size, current.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise RuntimeError("CSV 在保存期间被其他程序修改，已取消追加；请关闭 Excel 后重试。")
        os.replace(temp_path, output)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return len(additions)


def main():
    parser = argparse.ArgumentParser(description="只读提取上科大邮箱全部文件夹的邮件头，生成待标注 CSV。")
    parser.add_argument("--limit", type=int, help="可选：限制导出数量；默认全部读取")
    parser.add_argument("--inbox-only", action="store_true", help="仅读取收件箱；默认读取全部可选择文件夹")
    parser.add_argument("--output", help="new 或 CSV 路径；已有文件追加，不存在的路径新建")
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit 必须大于 0")
    try:
        output, append = choose_output(args.output)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"{'追加到' if append else '新建'}：{output.resolve()}")

    account = input("请输入完整校园邮箱地址：").strip()
    if not account.lower().endswith("@shanghaitech.edu.cn"):
        parser.error("本工具目前面向 @shanghaitech.edu.cn 邮箱。")
    password = getpass.getpass("请输入客户端专用密码（输入时不显示字符）：")
    if not password:
        parser.error("专用密码不能为空")
    try:
        with imaplib.IMAP4_SSL("imap.shanghaitech.edu.cn", 993,
                              ssl_context=ssl.create_default_context(), timeout=30) as client:
            print("正在连接并登录……")
            client.login(account, password)
            password = None
            rows = collect_headers(client, args.limit, args.inbox_only)
        if not rows:
            print("没有可导出的邮件，未生成文件。")
            return 0
        added = save_csv(rows, output, append)
        print(f"完成：读取 {len(rows)} 封，{'新增' if append else '写入'} {added} 条，文件位置：{output.resolve()}")
        print("用 Excel 打开 CSV，在 label 列填 1 或 0；不确定的先留空。")
        return 0
    except imaplib.IMAP4.error:
        print("邮箱登录或访问失败：请检查完整邮箱地址、客户端专用密码及 IMAP 权限。", file=sys.stderr)
    except PermissionError:
        print("文件无法读写：请关闭正在打开该 CSV 的 Excel，然后重试。", file=sys.stderr)
    except (socket.timeout, TimeoutError, OSError):
        print("连接或保存失败：请检查网络、证书、目标文件权限，以及文件是否已存在。", file=sys.stderr)
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\n已取消。")
        raise SystemExit(130)
