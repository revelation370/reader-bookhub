# -*- coding: utf-8 -*-
"""正文质量断言库（回归测试与一键体检共用）。

历史事故均来自四类问题，每类对应一组断言：
- 空壳/失败页（错抓导航区、熔断快速失败页）
- 正文混入导航/推广/水印（qbxs/yqyp 系）
- 全文拼成一行（</p> 不换行事故）
- 章节容器错位/匹配多节点
"""

# 正文开头 100 字内不得出现（导航/水印/失败提示特征词）
HEAD_BLACKLIST = (
    "加入书架", "章节目录", "下一页", "上一章", "下一章", "保存书签",
    "免费阅读", "笔趣阁", "手机访问", "手机浏览器", "正在加载", "抓取失败",
    "返回目录", "小说详情", "最新章节", "全文阅读", "无弹窗", "二维码",
    "当前位置", "您的位置", "作品编号", "推荐票", "一秒记住",
)

# 全文任何位置不得出现的高置信水印
BODY_BLACKLIST = (
    "当前浏览器转码失败", "访问验证", "本章未完，请点击下一页",
)

MIN_LEN = 200          # 正文最短字数（短章按失败处理）
MIN_PARA_RATIO = 1/600  # 换行数下限（防"全文一行"回归）


def check_chapter_text(text, head_look=100):
    """返回问题列表（空列表 = 通过）。text 为提取后的正文。"""
    problems = []
    if not text or len(text) < MIN_LEN:
        problems.append("正文过短(%s字)" % (len(text) if text else 0))
        return problems
    head = text[:head_look]
    for w in HEAD_BLACKLIST:
        if w in head:
            problems.append("开头含杂质词「%s」" % w)
    for w in BODY_BLACKLIST:
        if w in text:
            problems.append("正文含水印「%s」" % w)
    if text.count("\n") < len(text) * MIN_PARA_RATIO:
        problems.append("疑似全文一行（分段缺失）")
    return problems
