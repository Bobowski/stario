import stario.responses as responses
from stario import App, Span


async def plaintext(_c, w):
    responses.text(w, "Hello, World!")


async def bootstrap(app: App, span: Span):
    app.get("/plaintext", plaintext)
    yield
