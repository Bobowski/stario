from stario import Stario
from stario.datastar import Actions, Attributes, FileSignal, Signal
from stario.html import button, div, form, input_, label, p
from stario.toys import toy_page

app = Stario()


@app.query("/upload")
async def upload_page(attr: Attributes, act: Actions):
    """Render image upload page with signal-based file handling."""
    return toy_page(
        form(
            attr.signals(
                {
                    "files": [],
                    "success": False,
                    "errors": {},
                }
            ),
            # Image Upload Section
            div(
                p("Upload Images"),
                label(
                    p("Choose image files (max 5MB each)"),
                    input_(
                        {
                            "type": "file",
                            "accept": "image/*",
                            "multiple": True,
                        },
                        # This will automatically bind base64 enc file into signal
                        attr.bind("files"),
                    ),
                ),
                # Error display
                div(
                    attr.show("$errors.files"),
                    p(attr.text("$errors.files")),
                ),
            ),
            # Success Message
            div(
                attr.show("$success"),
                p("Images uploaded successfully!"),
            ),
            # Submit Button
            button(
                {"type": "button"},
                attr.attr({"disabled": "$uploading || !$files.length"}),
                attr.on("click", act.post("/upload-images")),
                attr.text("$uploading ? 'Uploading...' : 'Upload Images'"),
                attr.indicator("uploading"),
            ),
            # Loading indicator
            div(
                attr.show("$uploading"),
                p("Uploading images..."),
            ),
        ),
    )


@app.command("/upload-images")
async def upload_images(files: Signal[list[FileSignal]]):
    """Process and save uploaded images."""
    if not files:
        yield {"errors": {"files": "No images selected"}}
        return

    for file_info in files:

        # VALIDATION:
        # Let's just check the file size here:
        if file_info.size() > (5 * 1024 * 1024):  # 5MB:
            # You could also just patch the html elements
            #  rather than updating the signals - up to you!
            error_msg = f"File too large: {file_info.name} ({file_info.size() / 1024 / 1024:.1f}MB > 5MB)"
            yield {"errors": {"files": error_msg}}
            return

        # PROCESSING:
        # Do anytning with the file here...
        # Process, save, schedule etc...

    # Success! Clear form and show results
    yield {
        "files": [],
        "success": True,
        "errors": {},
    }
