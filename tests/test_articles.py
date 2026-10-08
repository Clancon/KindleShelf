from kindle_shelf.articles import ArticleExtractor


def test_extracts_wechat_content_and_lazy_image():
    article = ArticleExtractor.parse(
        """
        <html><head><meta property="og:title" content="一篇微信文章"><meta name="author" content="作者甲"></head>
        <body><div id="js_content"><p>这是需要保留的正文，长度足够用于识别。</p><img data-src="/cover.jpg"><script>bad()</script></div></body></html>
        """,
        "https://example.com/path/article",
    )
    assert article.title == "一篇微信文章"
    assert article.author == "作者甲"
    assert "这是需要保留的正文" in article.content_html
    assert 'src="https://example.com/cover.jpg"' in article.content_html
    assert "script" not in article.content_html


def test_generic_page_prefers_article():
    article = ArticleExtractor.parse(
        "<html><title>标题</title><body><nav>"
        + "链接" * 200
        + "</nav><article><h1>标题</h1><p>"
        + "正文" * 100
        + "</p></article></body></html>",
        "https://example.com/a",
    )
    assert article.title == "标题"
    assert "正文" in article.content_html
