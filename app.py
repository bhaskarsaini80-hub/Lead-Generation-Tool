from flask import Flask, send_from_directory, request, jsonify, send_file
from scraper import LocalBusinessScraper
import os
import zipfile
import re

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')
os.makedirs(STATIC_DIR, exist_ok=True)

app = Flask(__name__)

ACTIVE_SCRAPER = None
LAST_RESULTS = None


def sanitize_part(text):
    if not text:
        return "Unknown"
    cleaned = re.sub(r'[^\w\s-]', '', str(text)).strip()
    cleaned = re.sub(r'[\s-]+', '_', cleaned)
    return cleaned[:35] or "Unknown"


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return response


@app.route('/')
def index():
    if os.path.exists(os.path.join(BASE_DIR, 'index.html')):
        return send_from_directory(BASE_DIR, 'index.html')
    elif os.path.exists(os.path.join(BASE_DIR, 'templates', 'index.html')):
        return send_from_directory(os.path.join(BASE_DIR, 'templates'), 'index.html')
    return "Error: index.html file nahi mili!", 404


@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "message": "Backend is running on Render!"}), 200


@app.route('/api/run-automation', methods=['POST', 'OPTIONS'])
def run_automation():
    global ACTIVE_SCRAPER, LAST_RESULTS
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200

    # Agar pehle se koi scraper chal raha hai toh usey rokein
    if ACTIVE_SCRAPER is not None:
        try:
            ACTIVE_SCRAPER.request_stop()
        except Exception:
            pass

    # Purani report files saaf karein
    for f in os.listdir(STATIC_DIR):
        if f.endswith(('.xlsx', '.pdf', '.zip')):
            try:
                os.remove(os.path.join(STATIC_DIR, f))
            except Exception:
                pass

    data = request.json or {}
    tasks = data.get('tasks', [])

    # Render par hamesha headless=True aur safe concurrency (max 2) rakhein
    headless = True
    visit_website = data.get('visit_website', True)
    parallel = data.get('parallel', False)
    raw_conc = int(data.get('concurrency', 1) or 1)
    concurrency = min(2, max(1, raw_conc))
    max_leads_per_task = int(data.get('max_leads_per_task', 0) or 0)
    size_filter = data.get('size_filter', 'all')

    if not tasks:
        keyword = data.get('keyword', 'zym').strip()
        location = data.get('location', 'Delhi, India').strip()
        country = data.get('country', 'India').strip()
        phone_prefix = data.get('phone_prefix', '').strip()
        tasks = [{
            "keyword": keyword,
            "location": location,
            "country": country,
            "phone_prefix": phone_prefix,
            "max_leads": max_leads_per_task,
            "size_filter": size_filter
        }]

    for t in tasks:
        t['visit_website'] = visit_website
        if 'size_filter' not in t or not t['size_filter']:
            t['size_filter'] = size_filter

    try:
        ACTIVE_SCRAPER = LocalBusinessScraper(
            tasks=tasks,
            headless=headless,
            visit_website=visit_website,
            parallel=parallel,
            concurrency=concurrency,
            max_leads_per_task=max_leads_per_task,
            size_filter=size_filter
        )
        grouped = ACTIVE_SCRAPER.search_google_maps()
        ACTIVE_SCRAPER.generate_reports(STATIC_DIR)

        was_stopped = grouped.get("stopped", False)
        total_tasks = len(tasks)

        LAST_RESULTS = {
            "status": "success",
            "stopped": was_stopped,
            "is_multi": total_tasks > 1,
            "total_tasks": total_tasks,
            "count": len(grouped["all"]),
            "with_website_count": len(grouped["with_website"]),
            "without_website_count": len(grouped["without_website"]),
            "data": grouped["all"],
            "with_website": grouped["with_website"],
            "without_website": grouped["without_website"],
            "tasks": grouped.get("tasks_meta", [])
        }
        ACTIVE_SCRAPER = None
        return jsonify(LAST_RESULTS)

    except Exception as e:
        ACTIVE_SCRAPER = None
        print(f"[!] Backend Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/stop-automation', methods=['POST', 'OPTIONS'])
def stop_automation():
    global ACTIVE_SCRAPER
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200
    if ACTIVE_SCRAPER is not None:
        ACTIVE_SCRAPER.request_stop()
        return jsonify({"status": "stopping", "message": "Stop signal sent!"})
    return jsonify({"status": "idle", "message": "No active scraper running."})


@app.route('/api/skip-current-task', methods=['POST', 'OPTIONS'])
def skip_current_task():
    global ACTIVE_SCRAPER
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200
    if ACTIVE_SCRAPER is not None:
        ACTIVE_SCRAPER.skip_current_Running_task()
        return jsonify({"status": "skipping", "message": "Skipping current task..."})
    return jsonify({"status": "idle", "message": "No active scraper running."})


@app.route('/api/stop-task/<int:task_id>', methods=['POST', 'OPTIONS'])
def stop_single_task(task_id):
    global ACTIVE_SCRAPER
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200
    if ACTIVE_SCRAPER is not None:
        ACTIVE_SCRAPER.request_stop_task(task_id)
        return jsonify({"status": "stopping", "message": f"Stop requested for task #{task_id}"})
    return jsonify({"status": "idle", "message": "No active scraper running."})


@app.route('/api/scraper-progress', methods=['GET'])
def scraper_progress():
    global ACTIVE_SCRAPER, LAST_RESULTS
    if ACTIVE_SCRAPER is not None:
        meta = ACTIVE_SCRAPER.get_tasks_meta_progress()
        return jsonify({
            "running": True,
            "parallel": ACTIVE_SCRAPER.parallel,
            "active_tasks_count": meta["active_tasks_count"],
            "total_tasks": len(ACTIVE_SCRAPER.tasks_meta),
            "count": len(ACTIVE_SCRAPER.results),
            "with_website_count": len(ACTIVE_SCRAPER.with_website),
            "without_website_count": len(ACTIVE_SCRAPER.without_website),
            "data": ACTIVE_SCRAPER.results,
            "with_website": ACTIVE_SCRAPER.with_website,
            "without_website": ACTIVE_SCRAPER.without_website,
            "tasks": meta["tasks"]
        })
    elif LAST_RESULTS is not None:
        return jsonify({
            "running": False,
            **LAST_RESULTS
        })
    return jsonify({"running": False})


@app.route('/download/<category>/<file_type>')
def download_report(category, file_type):
    task_id = request.args.get('task_id')
    biz = sanitize_part(request.args.get('biz', 'Business'))
    country = sanitize_part(request.args.get('country', 'Location'))

    ext = 'xlsx' if file_type == 'excel' else 'pdf'
    cat_key = category if category in ('with_website', 'without_website', 'all') else 'all'
    label_map = {
        'with_website': 'With_Website',
        'without_website': 'Without_Website',
        'all': 'All_Leads'
    }
    readable_suffix = label_map[cat_key]

    if task_id:
        disk_filename = f"task_{task_id}_{cat_key}.{ext}"
        download_name = f"{biz}_{country}_{readable_suffix}.{ext}"
        file_path = os.path.join(STATIC_DIR, disk_filename)
        if os.path.exists(file_path):
            return send_file(file_path, as_attachment=True, download_name=download_name)

    master_filename = f"leads_{cat_key}.{ext}"
    master_path = os.path.join(BASE_DIR, 'static', master_filename)
    if os.path.exists(master_path):
        return send_file(master_path, as_attachment=True, download_name=f"{biz}_{country}_{readable_suffix}.{ext}")

    return "Report file abhi generate nahi hui hai.", 404


@app.route('/download-batch-zip')
def download_batch_zip():
    zip_path = os.path.join(STATIC_DIR, "All_Multi_Business_Leads_Reports.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(STATIC_DIR):
            for file in files:
                if file.endswith(('.xlsx', '.pdf')) and file != "All_Multi_Business_Leads_Reports.zip":
                    zipf.write(os.path.join(root, file), file)

    if os.path.exists(zip_path):
        return send_file(zip_path, as_attachment=True, download_name="All_Multi_Business_Leads_Reports.zip")
    return "ZIP file generate nahi ho payi.", 404


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    print("====================================================")
    print(f"Server Chalu Ho Gaya Hai on Port {port}!")
    print("====================================================")
    app.run(debug=False, threaded=True, host='0.0.0.0', port=port)
