"""Launch the Gradio app."""
from app.ui import build_app


def main():
    app = build_app()
    app.launch()


if __name__ == "__main__":
    main()
