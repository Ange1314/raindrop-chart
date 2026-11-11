"""锁定：让指定节点在接口层彻底不出现。

密码不落盘 —— 磁盘上只有 salt 和 check 两样东西：
    derived = PBKDF2-HMAC-SHA256(密码, salt, 600000 轮, 32 字节)
    check   = SHA256(derived)
验证时重算一遍，比对 check。磁盘上永远找不到密码，也反推不出来
（PBKDF2 单向、SHA256 单向）。

★ 它防得住什么，防不住什么，必须写清楚，不许含糊：
  防得住：随手翻电脑的人、自动扫描目录的 AI、别的程序想通过接口拿数据。
  防不住：能改你本地文件的人 —— 他可以把 check 换成自己密码算出来的。
          要防这一层只能把 check 放云端，但那就断网锁死了，不值。
  不涉及：文件本身。锁只作用在接口层，文件一个字节都不动。
          「文件锁」那条路已经实测过：ACL 会把自己账户一起锁死且撤不回来。

锁的粒度用 uid（rel: / sid: / wid: / kind:at）。父被锁时整棵子树一起不返回。
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading

ITER = 600000          # OWASP 2023 对 PBKDF2-HMAC-SHA256 的推荐下限
SALT_LEN = 16
KEY_LEN = 32


def _b64(b):
    return base64.b64encode(b).decode("ascii")


def _unb64(s):
    try:
        return base64.b64decode(s.encode("ascii"))
    except Exception:
        return b""


def _derive(pwd, salt):
    return hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, ITER, KEY_LEN)


def _check_of(derived):
    return hashlib.sha256(derived).digest()


class LockBox:
    """一个文件、一把锁、一份被锁清单。"""

    def __init__(self, path):
        self.path = path
        self._mu = threading.Lock()
        self.salt = b""
        self.check = b""
        self.locked = set()
        self._load()

    # ---------- 落盘 ----------

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return
        self.salt = _unb64(d.get("salt") or "")
        self.check = _unb64(d.get("check") or "")
        self.locked = set(d.get("locked") or [])

    def _save(self):
        # 只写这三样：salt / check / locked。密码不在里面，也没有它的任何形式。
        d = {
            "ver": 1,
            "salt": _b64(self.salt) if self.salt else "",
            "check": _b64(self.check) if self.check else "",
            "locked": sorted(self.locked),
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ---------- 密码 ----------

    def has_pwd(self):
        return bool(self.salt and self.check)

    def verify(self, pwd):
        if not self.has_pwd() or not pwd:
            return False
        # compare_digest 防时序攻击：逐字节比较会让攻击者按耗时猜出前缀
        return hmac.compare_digest(_check_of(_derive(pwd, self.salt)), self.check)

    def set_pwd(self, old, new):
        """首次设置时 old 传空；改密码必须带对的 old。"""
        if not new or len(new) < 4:
            return False, "密码至少 4 位"
        with self._mu:
            if self.has_pwd() and not self.verify(old):
                return False, "原密码不对"
            self.salt = secrets.token_bytes(SALT_LEN)
            self.check = _check_of(_derive(new, self.salt))
            self._save()
        return True, ""

    # ---------- 锁定 ----------

    def is_locked(self, uid):
        return bool(uid) and uid in self.locked

    def node_uid(self, node):
        """取节点用来比对的身份。和前端 addrSeg 的优先级保持一致：
        谁的字段更稳定就用谁，不要各写一套。"""
        if not node:
            return ""
        for k in ("uid", "rel", "wid", "sid", "srcKey"):
            v = node.get(k)
            if v:
                return ("%s:%s" % (k, v)) if k == "rel" else str(v)
        return ""

    def toggle(self, pwd, uid):
        if not self.verify(pwd):
            return False, "密码不对", False
        if not uid:
            return False, "这个节点没有身份，锁不了", False
        with self._mu:
            if uid in self.locked:
                self.locked.discard(uid)
                on = False
            else:
                self.locked.add(uid)
                on = True
            self._save()
        return True, "", on

    def state(self):
        return {"has_pwd": self.has_pwd(), "locked": sorted(self.locked),
                "count": len(self.locked)}

    def reset(self):
        """忘密码的唯一出路：清空密码 + 清空所有锁定。

        ★ 故意不要求密码，理由必须写在代码里，否则以后有人会来「加固」：
          `_lock.json` 就在用户自己的硬盘上，能物理接触这台机器的人
          本来就能直接删掉它。假装需要一个连我们自己都验证不了的凭据，
          只是把**真正的所有者**挡在门外，防不住任何人。

        重置的后果是「锁全部打开」，不是「绕过锁」——
        它降低的是保护强度，不是安全性门槛，两者不能混为一谈。
        """
        with self._mu:
            self.salt = b""
            self.check = b""
            self.locked = set()
            try:
                if os.path.exists(self.path):
                    os.remove(self.path)
            except Exception:
                pass
        return True

    # ---------- 过滤 ----------

    def keep(self, node):
        """一棵树：被锁的节点连同子树一起剪掉。返回 None 表示整棵不返回。

        ★ 必须返回**新对象**，不能原地删 children。
          树是数据源里的缓存对象（src.root() 每次给同一个），原地删等于
          把锁定写进了缓存 —— 解锁之后那些节点再也回不来，只能重启。
        """
        if not node or not self.locked:
            return node
        if self.is_locked(self.node_uid(node)):
            return None
        kids = node.get("children")
        if kids:
            out = dict(node)
            out["children"] = [c for c in (self.keep(c) for c in kids) if c]
            return out
        return node

    def drop(self, nodes):
        """一组同级节点：直接过滤。"""
        if not nodes or not self.locked:
            return nodes
        return [c for c in (self.keep(n) for n in nodes) if c]

    def blocked(self, key):
        """按 uid 直接问「这个能不能给」。会话明细、正文搜索这类没有树的地方用它。"""
        return self.is_locked(key)
