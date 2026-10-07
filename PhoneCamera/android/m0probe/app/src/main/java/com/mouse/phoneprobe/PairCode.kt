package com.mouse.phoneprobe

/**
 * 配对码解析 —— **手输 / 粘贴 / 扫码三条路径共用的唯一解析器**。
 *
 * 为什么必须只有一份：若扫码走一条解析、手输走另一条，现场就会出现
 * 「扫到的能连、粘同一行 URL 连不上」这种没法自证的分叉，两条路还会各自演化
 * ⇒ 契约漂移（本项目的登记表与锚定测试防的正是这个）。
 * 所以 [DeliverySession.connect] 也走这里，**不允许第二套**。
 *
 * 接受三种写法（都归一到同一个 [Result]）：
 *  1. `m0probe://pair?h=<host>&p=<port>&t=<token>` —— PC 端二维码/面板用的**规范**写法；
 *  2. `http://<host>:<port>/?token=<token>` —— PC 终端打印的整行 URL（旧码兼容）；
 *  3. `<host>[:<port>]` —— 手工只填地址（token 单填）。
 *
 * **明确拒绝**其它一切（`weixin://…`、`WIFI:…`、`https://mp.weixin.qq.com/s/xxx`、
 * 含空格的一句话…）。理由是防误扫优先于宽容：
 * 把公众号链接塞进地址框的结果是「连接失败」，而用户看到的只是「扫码没反应」，
 * 排查方向一开始就错了。**说不出"这不是配对码"的解析器，等于把错误藏起来。**
 *
 * 契约锚定：`pc/test_server.py` 第 26 组会**从本文件源码里读**
 * [SCHEME] / [ACTION] / [KEY_HOST] / [KEY_PORT] / [KEY_TOKEN] / [DEF_PORT]
 * 以及 [SELFTEST] 里的样本串，断言 PC 生成器 `pair_uri()` 产出的
 * 正是这里认得的写法（铁律：夹具/格式串必须从被观测对象的真实产出导出）。
 * ⇒ 这里改写法会立刻弄红 PC 侧测试，而不是等到现场扫不出来。
 */
object PairCode {

    // ---- 规范配对码的字面量（**被 PC 侧测试锚定，改动要同步 PC 生成器**）----
    const val SCHEME = "m0probe"
    const val ACTION = "pair"
    const val KEY_HOST = "h"
    const val KEY_PORT = "p"
    const val KEY_TOKEN = "t"

    /** 缺省端口。必须与 `pc/server.py` 的 `--port` 默认值一致（由锚定测试把关）。 */
    const val DEF_PORT = 8765

    /** token 长度上限：PC 端是 `secrets.token_hex(4)`（8 字符），留足余量。 */
    const val MAX_TOKEN_LEN = 64

    /** 解析来源。日志与判据都要能分清"用户手输"还是"扫到的"。 */
    const val SRC_SCHEME = "scheme"       // m0probe://pair?...
    const val SRC_URL = "url"             // http(s)://host:port/?token=...
    const val SRC_HOSTPORT = "hostport"   // host[:port]

    /** 二维码里那串原文（供日志/复现）。 */
    val extraText = "pair_text"

    data class Result(
        val ok: Boolean,
        val host: String = "",
        val port: Int = DEF_PORT,
        val token: String = "",
        val source: String = "",
        /** ok=false 时的原因，**要能直接显示给用户**（不许写"解析失败"这种废话）。 */
        val reason: String = "",
        /** 正常但需要人知道的事（如"没带 token"），照实说出来，不静默。 */
        val warnings: List<String> = emptyList(),
    ) {
        /** 地址框该填什么（与 [DeliverySession.connect] 的输入格式一致）。 */
        fun target(): String = if (host.isEmpty()) "" else "$host:$port"

        /** 日志里用的脱敏 token（**不打印完整 token**：日志会被复制给别人看）。 */
        fun tokenMasked(): String = if (token.isEmpty()) "（空）" else token.take(4) + "…"

        fun describe(): String =
            if (!ok) "解析失败：$reason"
            else "host=$host port=$port token=${tokenMasked()} 来源=$source" +
                warnings.joinToString("") { "；$it" }
    }

    /** 自检向量的一条。 */
    data class Case(
        val input: String,
        val ok: Boolean,
        val host: String = "",
        val port: Int = DEF_PORT,
        val token: String = "",
        /** 反例：期望 reason 里包含这个关键词（防止"因为别的原因恰好失败"也算过）。 */
        val why: String = "",
    )

    // ------------------------------------------------------------------ 解析

    fun parse(raw: String): Result {
        val s = raw.trim().trim('"', '\'', '“', '”', '‘', '’', '\u200B', '\uFEFF').trim()
        if (s.isEmpty()) return bad("内容为空")
        val lower = s.lowercase()
        val warn = ArrayList<String>()

        // ---- ① 规范配对码：m0probe://pair?h=..&p=..&t=.. ----
        if (lower.startsWith("$SCHEME:") || lower == SCHEME) {
            var rest = s.substring(SCHEME.length + 1)
            while (rest.startsWith("/")) rest = rest.substring(1)
            val q = rest.indexOf('?')
            val head = (if (q >= 0) rest.substring(0, q) else rest).trim().trimEnd('/')
            val query = if (q >= 0) rest.substring(q + 1) else ""
            if (head.isNotEmpty() && !head.equals(ACTION, ignoreCase = true)) {
                return bad("配对码动作不对：$SCHEME://$head（应为 $SCHEME://$ACTION）")
            }
            val kv = parseQuery(query)
            val hostRaw = kv[KEY_HOST] ?: kv["host"] ?: ""
            val portRaw = kv[KEY_PORT] ?: kv["port"] ?: ""
            val tokenRaw = kv[KEY_TOKEN] ?: kv["token"] ?: ""
            if (hostRaw.isEmpty()) {
                return bad("配对码里没有主机地址（缺 $KEY_HOST= 或 host= 参数）")
            }
            val host = checkHost(hostRaw) ?: return bad("配对码里的主机地址不合法：$hostRaw")
            val port = parsePort(portRaw) ?: return bad("配对码里的端口不合法：$portRaw")
            val token = checkToken(tokenRaw, warn)
            if (token.isEmpty()) warn.add(noTokenNote())
            return Result(true, host, port, token, SRC_SCHEME, "", warn)
        }

        // ---- ② 旧写法：整行 http(s) URL ----
        val m = SCHEME_RE.find(s)
        if (m != null) {
            val sch = m.groupValues[1].lowercase()
            if (sch != "http" && sch != "https") {
                return bad("这不是配对码（看起来是一个 $sch:// 链接）")
            }
            var rest = s.substring(m.value.length)
            var tokenRaw = ""
            val q = rest.indexOf('?')
            if (q >= 0) {
                val kv = parseQuery(rest.substring(q + 1))
                tokenRaw = kv["token"] ?: kv[KEY_TOKEN] ?: ""
                rest = rest.substring(0, q)
            }
            val slash = rest.indexOf('/')
            if (slash >= 0) rest = rest.substring(0, slash)
            val auth = splitAuthority(rest) ?: return bad("URL 里没有主机地址")
            val host = checkHost(auth.first) ?: return bad("URL 里的主机地址不合法：${auth.first}")
            val port = parsePort(auth.second) ?: return bad("URL 里的端口不合法：${auth.second}")
            val token = checkToken(tokenRaw, warn)
            // 「是不是局域网」决定"没带 token 的链接"能不能算配对码：
            // 公众号/文章二维码都是**有路径、无 token 的公网链接**，必须拒绝；
            // 而 `http://192.168.1.23:8765/` 这种是用户自己贴的地址，要放行。
            val lan = isLanLike(host)
            if (token.isEmpty()) {
                if (!lan) {
                    return bad("这是网页链接，不是配对码（既没有 token，主机 $host 也不是局域网地址）")
                }
                warn.add(noTokenNote())
            } else if (!lan) {
                warn.add("主机 $host 看起来不是局域网地址，确认无误再连")
            }
            warn.add("这是旧写法（整行 URL）；PC 端现在打印的是 $SCHEME:// 配对码")
            return Result(true, host, port, token, SRC_URL, "", warn)
        }

        // ---- ③ 裸 host[:port]（含 [IPv6]:port）----
        if (s.any { it.isWhitespace() }) {
            return bad("内容里有空格，不像地址（可能扫到了文章/名片码）")
        }
        val auth = splitAuthority(s) ?: return bad("内容不是主机地址：$s")
        val host = checkHost(auth.first) ?: return bad("内容不是主机地址：$s")
        val port = parsePort(auth.second) ?: return bad("端口不合法：${auth.second}")
        warn.add(noTokenNote())
        return Result(true, host, port, "", SRC_HOSTPORT, "", warn)
    }

    /** 与 PC 端 `pair_uri()` 一一对应的生成器（锚定测试的另一半）。 */
    fun build(host: String, port: Int = DEF_PORT, token: String): String =
        "$SCHEME://$ACTION?$KEY_HOST=$host&$KEY_PORT=$port&$KEY_TOKEN=$token"

    /** 缺 token 的统一说法（三处共用，避免三种写法三种口径）。 */
    private fun noTokenNote(): String =
        "内容里没带 token ⇒ 需要在 token 框里手输（PC 启动时会打印）"

    private fun bad(why: String) = Result(ok = false, reason = why)

    private val SCHEME_RE = Regex("^([A-Za-z][A-Za-z0-9+.\\-]*)://")
    private val HOST_RE = Regex("^[A-Za-z0-9._:\\-\\[\\]]+$")
    private val TOKEN_RE = Regex("^[A-Za-z0-9._\\-]+$")

    /**
     * `host[:port]` 拆分。
     *
     * ⚠ 冒号后面**不是数字**时，整串当主机名返回（而不是拆成 host+乱码端口）——
     * 因为 `WIFI:S=myssid;T=WPA;P=pass;;` 这类内容的冒号后面恰好也不是数字，
     * 交给 [checkHost] 的字符集校验去拒，比在这里猜更可靠。
     */
    private fun splitAuthority(a0: String): Pair<String, String>? {
        val a = a0.trim().trimEnd('/')
        if (a.isEmpty()) return null
        if (a.startsWith("[")) {                      // IPv6 字面量：[fe80::1]:8765
            val close = a.indexOf(']')
            if (close < 0) return null
            val h = a.substring(0, close + 1)
            val rest = a.substring(close + 1)
            return when {
                rest.isEmpty() -> h to ""
                rest.startsWith(":") -> h to rest.substring(1)
                else -> null
            }
        }
        val i = a.lastIndexOf(':')
        if (i < 0) return a to ""
        val p = a.substring(i + 1)
        return if (p.isNotEmpty() && p.all { it.isDigit() }) a.substring(0, i) to p else a to ""
    }

    private fun parseQuery(q0: String): Map<String, String> {
        val out = LinkedHashMap<String, String>()
        val q = q0.trim().removePrefix("?")
        if (q.isEmpty()) return out
        for (kv in q.split('&')) {
            val e = kv.split('=', limit = 2)
            val k = e[0].trim()
            if (k.isEmpty()) continue
            val v = if (e.size == 2) e[1].trim() else ""
            // 同名参数只认第一个：`?h=a&h=b` 若让后者悄悄覆盖前者，
            // 就等于允许"后面那串决定连到哪" —— 这种静默优先级是给攻击面开门。
            if (!out.containsKey(k)) out[k] = v
        }
        return out
    }

    /** 端口：空 ⇒ [DEF_PORT]；非法/越界 ⇒ null（调用方给出人话）。 */
    private fun parsePort(s0: String): Int? {
        val s = s0.trim()
        if (s.isEmpty()) return DEF_PORT
        val v = s.toIntOrNull() ?: return null
        return if (v in 1..65535) v else null
    }

    private fun checkHost(h0: String): String? {
        val h = h0.trim().trimEnd('/')
        if (h.isEmpty() || h.length > 253) return null
        if (!HOST_RE.matches(h)) return null
        if (h.startsWith("[") != h.endsWith("]")) return null     // IPv6 括号必须成对
        return h
    }

    /**
     * token 里出现意料之外的字符 ⇒ **丢弃并说明**，不当作 token 传下去。
     * 理由：token 是鉴权凭据，把 `?t=<script>` 这种东西当凭据发出去只会得到
     * 一个 403，而用户会以为"配对码是对的、是网络问题"。
     */
    private fun checkToken(t0: String, warn: MutableList<String>): String {
        val t = t0.trim()
        if (t.isEmpty()) return ""
        if (t.length > MAX_TOKEN_LEN || !TOKEN_RE.matches(t)) {
            warn.add("token 里有意料之外的字符，已忽略（避免把乱码当凭据）")
            return ""
        }
        return t
    }

    /**
     * 像"直连局域网地址"吗？判断依据只用于**放行/收紧"没带 token"的 http 链接**，
     * 不参与其它判据（避免它变成"能不能连"的权威 —— 那只有实连才能回答）。
     */
    private fun isLanLike(h0: String): Boolean {
        val h = h0.trim('[', ']')
        if (h.equals("localhost", ignoreCase = true)) return true
        if (h.endsWith(".local") || h.endsWith(".lan")) return true
        if (h.contains(':')) return true                  // IPv6 只可能是直连
        if (!h.contains('.')) return true                 // 无点主机名 ⇒ 局域网内解析
        val p = h.split('.')
        if (p.size == 4 && p.all { it.toIntOrNull()?.let { v -> v in 0..255 } == true }) {
            val a = p[0].toInt()
            val b = p[1].toInt()
            return a == 10 || a == 127 || (a == 192 && b == 168) || (a == 172 && b in 16..31) ||
                (a == 169 && b == 254) || (a == 100 && b in 64..127)
        }
        return false
    }

    // ------------------------------------------------------------------ 自检

    /**
     * 自检向量。**正例反例都必须在** —— 只有正例的表会在解析器退化成"什么都接受"
     * 时依然全绿（本项目的"真空通过"）。
     *
     * 第 1 条必须与 PC 端 `pair_uri()` 的产出**逐字符相同**：
     * `pc/test_server.py` 第 26 组会从本文件里正则抽出这条串，
     * 用 PC 侧生成器反算一遍再比对（双向锚定）。
     *
     * 长按「扫码配对」按钮会在真机上跑完整张表并把结果写进日志 ——
     * **现场的机器自己会证明解析器是好的**，不靠"我这边编译通过"。
     */
    val SELFTEST: List<Case> = listOf(
        // ---------- 正例：规范配对码 ----------
        Case("m0probe://pair?h=192.168.1.23&p=8765&t=A3F9C1D2E4B5", true,
            "192.168.1.23", 8765, "A3F9C1D2E4B5"),
        Case("m0probe://pair?h=192.168.1.23&p=9000&t=deadbeef", true,
            "192.168.1.23", 9000, "deadbeef"),
        Case("m0probe://pair?host=10.0.0.7&port=8765&token=abc123", true,
            "10.0.0.7", 8765, "abc123"),
        Case("M0PROBE://PAIR?h=10.0.0.7&p=8765&t=abc123", true, "10.0.0.7", 8765, "abc123"),
        Case("m0probe:pair?h=172.16.5.9&p=8765&t=c0ffee", true, "172.16.5.9", 8765, "c0ffee"),
        Case("m0probe://pair?h=192.168.1.23&p=8765", true, "192.168.1.23", 8765, ""),
        // ---------- 正例：旧写法（整行 URL） ----------
        Case("http://192.168.1.23:8765/?token=A3F9C1D2E4B5", true,
            "192.168.1.23", 8765, "A3F9C1D2E4B5"),
        Case("https://10.0.0.7:8765/?token=abc", true, "10.0.0.7", 8765, "abc"),
        Case("http://192.168.1.23:8765/", true, "192.168.1.23", 8765, ""),
        // ---------- 正例：裸地址 ----------
        Case("192.168.1.23:8765", true, "192.168.1.23", 8765, ""),
        Case("192.168.1.23", true, "192.168.1.23", 8765, ""),
        Case("pc-lab.local:8765", true, "pc-lab.local", 8765, ""),
        Case("[fe80::1]:8765", true, "[fe80::1]", 8765, ""),
        // ---------- 反例：必须拒绝，且原因要对 ----------
        Case("weixin://dl/business/?t=abc", false, why = "weixin"),
        Case("https://mp.weixin.qq.com/s/abcdefg", false, why = "网页链接"),
        Case("WIFI:S=myssid;T=WPA;P=pass;;", false, why = "不是主机地址"),
        Case("ftp://192.168.1.23:8765/", false, why = "ftp"),
        Case("m0probe://other?h=192.168.1.23&p=8765", false, why = "动作不对"),
        Case("m0probe://pair?h=&p=8765&t=abc", false, why = "没有主机地址"),
        Case("192.168.1.23:99999", false, why = "端口"),
        Case("这是一句中文", false, why = "不是主机地址"),
        Case("", false, why = "空"),
        Case("http://:8765/?token=abc", false, why = "主机地址"),
    )

    /**
     * 跑 [SELFTEST]。返回 (总数, 失败明细)。
     * 失败明细要能一眼看出"哪条输入、期望什么、实得什么"——现场日志里就靠它定位。
     */
    fun runSelfTest(): Pair<Int, List<String>> {
        val fails = ArrayList<String>()
        for (c in SELFTEST) {
            val r = parse(c.input)
            val why = when {
                r.ok != c.ok -> "ok 期望 ${c.ok} 实得 ${r.ok}（${if (r.ok) r.describe() else r.reason}）"
                !c.ok && c.why.isNotEmpty() && !r.reason.contains(c.why, ignoreCase = true) ->
                    "reason 应含「${c.why}」，实得「${r.reason}」"
                c.ok && r.host != c.host -> "host 期望 ${c.host} 实得 ${r.host}"
                c.ok && r.port != c.port -> "port 期望 ${c.port} 实得 ${r.port}"
                c.ok && r.token != c.token -> "token 期望 ${c.token} 实得 ${r.token}"
                else -> null
            }
            if (why != null) fails.add("输入「${c.input}」：$why")
        }
        return SELFTEST.size to fails
    }
}
