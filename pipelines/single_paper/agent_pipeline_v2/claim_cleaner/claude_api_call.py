"""
claude_api_call.py

可复用的 Claude API 调用统一封装(基于官方 anthropic SDK)。

项目里所有需要调用 Claude API 的地方,都应该 import 这个文件,不要各自
再直接 `import anthropic`、更不要自己手搓 HTTP 请求。

基本用法:
    from claude_api_call import call_claude

    result = call_claude("你的prompt内容")
    # result 是模型返回的文本;如果这一条被跳过(interrupt_on_error=True时,
    # 报错后人工菜单里选了's'),result 是 None——调用方不用做任何特殊处理,
    # 直接把 None 当正常返回值往下用即可,哪怕后面解析 JSON 会因为 None
    # 出错也不用额外防护。

    result = call_claude("你的prompt内容", max_tokens=5000)                 # 自定义max_tokens
    result = call_claude("你的prompt内容", thinking={"type": "disabled"})   # 关闭思考
    result = call_claude("你的prompt内容", system="你是一个只回答yes/no的助手")  # 系统提示词

非交互(不打断)用法——interrupt_on_error=False:
    默认(interrupt_on_error=True)遇到报错会打印提示并调用 input() 弹出
    "r 重试 / s 跳过"的人工菜单,适合有人盯着终端的批处理脚本。

    但有些场景是全自动跑一大批调用(比如逐个候选片段调用一次API做判断),
    单条失败不该、也不能打断整个流程去等人工输入。这种场景传
    interrupt_on_error=False:报错(400/401/403/429/5xx/连接失败等)不会
    打印提示、也不会调用 input(),而是把异常原样往外抛,交给调用方自己的
    try/except 处理、自行决定怎么降级。用法示例:

        try:
            result = call_claude("...", interrupt_on_error=False)
        except Exception:
            result = None  # 按失败处理,继续下一条

    注意:不管 interrupt_on_error 是 True 还是 False,下面说的"参数自动
    归一化"和"400错误自愈重试"都会照常生效——interrupt_on_error 只影响
    "自愈也解决不了、最终还是失败了"之后要不要打断等人工。

临时DeepSeek覆盖：显式设置已有DEEPSEEK_MODEL、DEEPSEEK_API_KEY（可选
DEEPSEEK_BASE_URL）时，同一入口改用DeepSeek，不依赖Anthropic SDK；原提示词、
清洗步骤和返回文本契约不变。所有重试使用该覆盖模型，Claude专用参数不透传。
未设置覆盖时，以下Claude行为保持不变。

需要提前设置好环境变量 ANTHROPIC_API_KEY(SDK 会自动读取,不用在代码里
传key)。注意:这个模块在 import 时**不会**去读取/校验这个环境变量,也
**不会**在 import 时就创建SDK客户端——只有真正发起第一次API调用时才会
去实例化客户端。这样即使运行环境完全没配key,只要代码路径上没有实际
触发 call_claude(),单纯 import 这个文件不会报错(方便像 divide_paragraphs
这种"没配key就跳过AI辅助功能、但其它功能应该正常跑"的场景)。

这个延迟创建的客户端显式设置了 timeout=600(10分钟)——这其实就是 SDK
自己的默认值,写出来只是不想依赖"以后SDK默认值会不会变"这件事。注意这个
timeout 是"两次收到数据之间最多能隔多久"(基于SDK底层httpx的读超时语义,
每收到一个字节就重新计时),不是"整个请求从头到尾最多能跑多久"——只要
还在陆续收到内容,哪怕总耗时远超10分钟也不会被这个设置打断。

调用API的方式内部统一走**流式**(SDK的 messages.stream() + 
get_final_message()),而不是一次性等完整响应——这对调用方完全透明,
call_claude() 的参数和返回值不受任何影响,不需要调用方做任何改动。这么
做是为了避免"思考或生成耗时很长的请求,期间连接长时间没有数据往回传"时
被网络中间设备当成空闲连接掐断:流式下模型每生成一点内容就有数据陆续
传回来,能大幅降低这个风险,SDK也不会因为预估耗时太长而拒绝这类请求。

参数容错说明(这是这个文件存在的核心目的):
调用方传进来的 model / max_tokens / thinking / system 这几个参数,不管
是缺省(None)、类型不对,还是用的是老版本 Claude API 的写法,这个文件都
会尽量自动纠正成当前 API 能接受的样子,而不需要调用方各自处理版本差异。
这套纠正分两层:

1. 静态归一化(发请求前就能判断出的问题):
   - model 不是非空字符串 -> 用默认模型
   - max_tokens 不是正整数 -> 用默认值
   - system 不是非空字符串 -> 不传这个字段
   - thinking 缺省、不是dict、或者"type"不是 enabled/disabled/adaptive
     三种已知取值之一 -> 不传"thinking"字段,让模型走自己的默认行为
   - thinking={"type": "enabled", ...}(老式手动预算写法,可能带
     budget_tokens)-> 自动转换成 {"type": "adaptive"}。
     注意:budget_tokens 和新API的 effort 不是等价换算关系(官方文档
     说明这是两个独立的控制项),这里不做臆测性数值映射;如果调用方在
     thinking字典里额外带了"effort"字段(提前用新写法),会被放进
     output_config.effort 里一并传过去。
   具体规则见 _normalize_request() 的注释。

2. 反应式自愈(发出去之后才知道的问题):如果请求被API以400拒绝,会去读
   错误信息里提到的具体字段,自动做针对性修正后重试(比如某模型不支持
   disabled就直接去掉这个字段;某模型不支持adaptive就退回老式enabled
   写法;某模型不支持某个采样参数就把它删掉)。每种修正在同一次调用里
   只会触发一次,不会死循环;修正后还是失败,或者判断不出该怎么修,就
   正常走下面的报错流程(人工菜单,或者 interrupt_on_error=False 时
   直接抛出)。

这两层能覆盖当前已知的版本差异,但不保证覆盖未来所有可能出现的新规则——
新模型会加什么新限制没法提前预知。这套机制的目标是"API小版本更新时大概率
不会直接报废",不是"以后永远不用管"。

额外参数:
call_claude() 支持透传任意额外的具名参数给 SDK 的 messages.create()(比如
以后官方新增了某个参数),不需要改这个文件的函数签名,调用方直接加关键字
参数传进来即可,例如 call_claude("...", stop_sequences=["END"])。

错误处理策略(interrupt_on_error=True,即默认情况):
所有错误(401、400、403,连接失败、客户端超时(注意 APITimeoutError 和
连接失败的 APIConnectionError 是两个独立的异常类、不是父子关系,这里两
个都会 catch、按同样的方式处理)、429/5xx等可自动恢复的错误、402、404、
413、409、422 等)统一处理:自动修正尝试都失败后,暂停,打印错误信息,
提供"r 重试 / s 跳过"两个选项,没有特殊分支。

注:这里说的"重试"都是指 SDK 外层的重试——SDK 自己内部对连接错误/超时/
429/5xx等已经会做几次自动重试,不管它内部试了几次,只要最终还是抛出异常
到这里,都按上面的策略统一处理,不再单独考虑"SDK内部重试了几次"这件事。
"""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_client = None
_DEFAULT_TIMEOUT_SECONDS = 600.0  # 10分钟,等同SDK自己的默认值,详见上面文档说明
_DEEPSEEK_TIMEOUT_SECONDS = 300.0  # DeepSeek urlopen 收紧到 5 分钟，超时显式抛错


def _get_client():
    """延迟创建SDK客户端:只有第一次真正调用API时才实例化,import这个
    模块本身不会因为没配置API key而报错。"""
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(timeout=_DEFAULT_TIMEOUT_SECONDS)
    return _client


def _ask_retry_or_skip() -> bool:
    """打印提示,等待人工输入。返回 True 表示重试,False 表示跳过。"""
    while True:
        choice = input("输入 r 重试 / s 跳过这一条: ").strip().lower()
        if choice == "s":
            return False
        if choice == "r":
            return True
        print("请输入 r 或 s")


_DEFAULT_MODEL = "claude-sonnet-5"
_DEFAULT_MAX_TOKENS = 2000
_VALID_THINKING_TYPES = {"enabled", "disabled", "adaptive"}


def _normalize_request(prompt, model, max_tokens, thinking, system, extra):
    """
    把调用方传进来的各种写法(缺省/类型不对/老版本API写法)统一整理成
    一份可以直接喂给 anthropic SDK 的 kwargs 字典。规则见模块顶部文档
    字符串"参数容错说明"第1条。

    返回 (kwargs, meta)。meta["original_thinking"] 保留调用方最初传入的
    thinking写法原样副本(仅当能识别出合法type时才会保留),供反应式自愈层
    在"新写法被模型拒绝"时回退用。
    """
    kwargs = {
        "model": model if isinstance(model, str) and model.strip() else _DEFAULT_MODEL,
        "max_tokens": (
            max_tokens
            if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0
            else _DEFAULT_MAX_TOKENS
        ),
        "messages": [{"role": "user", "content": prompt}],
    }

    if isinstance(system, str) and system.strip():
        kwargs["system"] = system

    meta = {"original_thinking": None}

    if (
        isinstance(thinking, dict)
        and isinstance(thinking.get("type"), str)
        and thinking["type"] in _VALID_THINKING_TYPES
    ):
        meta["original_thinking"] = dict(thinking)
        t_type = thinking["type"]

        if t_type == "enabled":
            # 老式手动预算写法 -> 转成新式 adaptive。不做 budget_tokens
            # 到 effort 的数值换算(两者不是等价关系),深度控制交给下面
            # 可选的 effort(如果调用方额外带了的话)或模型默认值。
            kwargs["thinking"] = {"type": "adaptive"}
        elif t_type == "disabled":
            kwargs["thinking"] = {"type": "disabled"}
        else:  # "adaptive"
            kwargs["thinking"] = {"type": "adaptive"}

        if t_type in ("enabled", "adaptive"):
            effort = thinking.get("effort")
            if isinstance(effort, str) and effort.strip():
                kwargs["output_config"] = {"effort": effort}
    # 其它情况(thinking是None,或者是不认识的格式)不设置thinking字段,
    # 让模型走自己的默认行为——这正是"参数格式错误时退回默认参数"的体现。

    if extra:
        kwargs.update(extra)

    return kwargs, meta


def _try_repair_400(error, kwargs, meta, tried):
    """
    尝试根据 400 错误信息里的关键词,对 kwargs 做一次针对性修正。

    tried 是这次 call_claude() 调用内部维护的 set,记录已经尝试过的修复
    类型,防止同一种修复反复触发导致死循环——每种修复类型在同一次调用里
    只会触发一次。

    能判断出修复方式就返回新的 kwargs 字典;判断不出具体原因、或者这种
    修复这次已经试过了,返回 None,交给上层走标准报错流程(人工菜单,或者
    interrupt_on_error=False 时直接抛出)。

    注意:这里的关键词匹配是根据官方文档里已公开的错误信息模式(形如
    `"thinking.type.xxx" is not supported`)做的尽量匹配,不是100%精确
    覆盖——Anthropic之后如果换了错误文案,这里可能就匹配不上了,会自然
    退回标准报错流程,不会误判成别的问题。
    """
    msg = str(error).lower()

    def mentions(*keywords):
        return all(k in msg for k in keywords)

    if mentions("thinking", "adaptive", "not supported") and "adaptive_unsupported" not in tried:
        # 当前模型是"只支持老式手动模式"的机型,不认 adaptive。
        tried.add("adaptive_unsupported")
        new_kwargs = dict(kwargs)
        new_kwargs.pop("output_config", None)
        original = meta.get("original_thinking") or {}
        if original.get("type") == "enabled":
            fixed = {"type": "enabled"}
            fixed["budget_tokens"] = (
                original["budget_tokens"] if isinstance(original.get("budget_tokens"), int) else 4096
            )
            new_kwargs["thinking"] = fixed
        else:
            new_kwargs.pop("thinking", None)
        return new_kwargs

    if mentions("thinking", "enabled", "not supported") and "enabled_unsupported" not in tried:
        # 当前模型不认老式手动enabled模式(4.7+/5系列的情况),转成adaptive。
        tried.add("enabled_unsupported")
        new_kwargs = dict(kwargs)
        new_kwargs["thinking"] = {"type": "adaptive"}
        return new_kwargs

    if mentions("thinking", "disabled", "not supported") and "disabled_unsupported" not in tried:
        # 当前模型强制思考,不允许关闭(比如部分新一代模型),直接去掉这个
        # 字段,让模型用自己的默认行为。
        tried.add("disabled_unsupported")
        new_kwargs = dict(kwargs)
        new_kwargs.pop("thinking", None)
        return new_kwargs

    if any(p in msg for p in ("temperature", "top_p", "top_k")) and "sampling_unsupported" not in tried:
        # 部分模型不允许自定义采样参数(比如要求必须用默认值)。
        tried.add("sampling_unsupported")
        new_kwargs = dict(kwargs)
        for p in ("temperature", "top_p", "top_k"):
            new_kwargs.pop(p, None)
        return new_kwargs

    if "effort" in msg and any(k in msg for k in ("disabled", "xhigh", "max")) and "effort_conflict" not in tried:
        # 思考关闭时,某些机型不允许把effort设成xhigh/max,降级成high。
        tried.add("effort_conflict")
        new_kwargs = dict(kwargs)
        oc = dict(new_kwargs.get("output_config") or {})
        if oc.get("effort") in ("xhigh", "max"):
            oc["effort"] = "high"
            new_kwargs["output_config"] = oc
            return new_kwargs
        return None

    return None


def call_claude(
    prompt: str,
    model: str | None = None,
    max_tokens: int | None = None,
    thinking: dict | None = None,
    system: str | None = None,
    interrupt_on_error: bool = True,
    **extra,
) -> str | None:
    # The pipeline's claim cleaner is provider-neutral at the call sites, but
    # in the Gaia run it must use DeepSeek Flash.  Keep the old function name
    # for compatibility with the cleaner modules and ignore their Claude model
    # names/retry tiers when DeepSeek credentials are present.
    if os.environ.get("DEEPSEEK_API_KEY"):
        override_model = "deepseek-v4-flash"
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("DeepSeek cleaning requires DEEPSEEK_API_KEY")
        base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        request = Request(f"{base}/chat/completions", data=json.dumps({
            "model": override_model, "messages": messages, "temperature": 0,
        }, ensure_ascii=False).encode("utf-8"), headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        }, method="POST")
        try:
            # Cleaner prompts can contain an entire claim batch.  A 180s
            # socket deadline caused Step 4 to fail while the upstream model
            # was still generating; five minutes is a middle ground, and a
            # socket timeout now surfaces as an explicit RuntimeError instead
            # of an uncaught TimeoutError (which would otherwise crash the
            # step subprocess with a confusing traceback).
            with urlopen(request, timeout=_DEEPSEEK_TIMEOUT_SECONDS) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"DeepSeek cleaning request failed with HTTP {exc.code}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"DeepSeek cleaning request timed out after {_DEEPSEEK_TIMEOUT_SECONDS}s") from exc
        except URLError as exc:
            raise RuntimeError(f"DeepSeek cleaning connection failed: {exc.reason}") from exc
        choice = raw["choices"][0]
        if choice.get("finish_reason") not in (None, "stop"):
            raise ValueError("DeepSeek cleaning response is incomplete")
        text = choice["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("DeepSeek cleaning returned empty text")
        return text

    import anthropic
    kwargs, meta = _normalize_request(prompt, model, max_tokens, thinking, system, extra)
    tried_repairs = set()

    while True:
        try:
            with _get_client().messages.stream(**kwargs) as stream:
                response = stream.get_final_message()
            # response.content 可能包含 ThinkingBlock(思考过程)等非文本内容块,
            # 不能假设第0块就是文本,要筛出所有 type=="text" 的块再拼起来
            text_blocks = [block.text for block in response.content if block.type == "text"]
            return "".join(text_blocks)

        except anthropic.APITimeoutError as e:
            # 客户端超时(两次收到数据之间隔太久——不是"整个请求跑太久了",
            # 详见模块顶部文档字符串关于timeout语义的说明)
            if not interrupt_on_error:
                raise
            print(f"[超时] {e}")
            if not _ask_retry_or_skip():
                return None
            continue

        except anthropic.APIConnectionError as e:
            # 连接失败(无状态码,且不是上面那种超时)
            if not interrupt_on_error:
                raise
            print(f"[连接错误] {e}")
            if not _ask_retry_or_skip():
                return None
            continue

        except anthropic.APIStatusError as e:
            # 所有带状态码的错误:401/400/403/429/5xx(含504/529等)/402/404/413/409/422 等
            if e.status_code == 400:
                repaired = _try_repair_400(e, kwargs, meta, tried_repairs)
                if repaired is not None:
                    kwargs = repaired
                    continue  # 用修正后的参数立刻重试,不打印、不计入人工菜单

            if not interrupt_on_error:
                raise
            print(f"[{e.status_code} 错误] {e}")
            if not _ask_retry_or_skip():
                return None
            continue
