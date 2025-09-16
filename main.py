from flask import Flask, render_template, request
from flask_cors import CORS
import json
import os

app = Flask(__name__)
CORS(app)

LINKS_PATH = "overlay_links.json"
CONFIG_PATH = "overlay_config.json"

# Stałe linki do overlayów
with open(LINKS_PATH) as f:
    OVERLAY_LINKS = json.load(f)


@app.route("/")
def index():
    return render_template("index.html", links=OVERLAY_LINKS)


@app.route("/kort/<kort_id>")
def overlay_kort(kort_id):
    kort_id = str(kort_id)

    if kort_id not in OVERLAY_LINKS:
        return f"Nieznany kort {kort_id}", 404

    # HOT reload konfiguracji przy każdym żądaniu
    with open(CONFIG_PATH) as f:
        overlay_config = json.load(f)

    main_overlay = OVERLAY_LINKS[kort_id]["overlay"]
    mini = [(k, v["overlay"]) for k, v in OVERLAY_LINKS.items() if k != kort_id]

    return render_template(
        "kort.html",
        kort_id=kort_id,
        main_overlay=main_overlay,
        mini_overlays=mini,
        config=overlay_config
    )


@app.route("/kort/all")
def overlay_all():
    """Renderuje widok z czterema kortami rozmieszczonymi w rogach."""
    with open(CONFIG_PATH) as f:
        overlay_config = json.load(f)

    corner_positions = [
        {"name": "top-left", "style": "top: 0; left: 0;"},
        {"name": "top-right", "style": "top: 0; right: 0;"},
        {"name": "bottom-left", "style": "bottom: 0; left: 0;"},
        {"name": "bottom-right", "style": "bottom: 0; right: 0;"},
    ]

    overlays = []
    sorted_overlays = sorted(
        OVERLAY_LINKS.items(),
        key=lambda item: int(item[0]) if str(item[0]).isdigit() else item[0]
    )

    for (kort_id, data), position in zip(sorted_overlays, corner_positions):
        overlays.append(
            {
                "id": kort_id,
                "overlay": data["overlay"],
                "position": position,
            }
        )

    return render_template(
        "kort_all.html",
        overlays=overlays,
        config=overlay_config,
    )


@app.route("/config", methods=["GET", "POST"])
def config():
    if request.method == "POST":
        data = {
            "view_width": int(request.form["view_width"]),
            "view_height": int(request.form["view_height"]),
            "display_scale": float(request.form["display_scale"]),
            "left_offset": int(request.form["left_offset"]),
            "label_position": request.form["label_position"]

        }
        with open(CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2)
        return render_template("config.html", config=data)

    # GET – pokaż aktualny config
    with open(CONFIG_PATH) as f:
        data = json.load(f)
    return render_template("config.html", config=data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
