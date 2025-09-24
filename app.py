from flask import Flask, request, render_template
from dotenv import load_dotenv
from pyairtable import Api
from google import genai
import os
from wordpress_xmlrpc import Client, WordPressPost
from wordpress_xmlrpc.methods import media
from wordpress_xmlrpc.methods.posts import NewPost
from wordpress_xmlrpc.compat import xmlrpc_client
from wordpress_xmlrpc.methods.posts import GetPost

# ===== FLASK SETUP =====
app = Flask(__name__)

# ===== CONFIG =====
load_dotenv()
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WORDPRESS_USER = os.getenv("WORDPRESS_USER")
WORDPRESS_APP_PASSWORD = os.getenv("WORDPRESS_APP_PASSWORD")
WORDPRESS_URL = os.getenv("WORDPRESS_URL")

# ===== INIT CLIENTS =====
api = Api(AIRTABLE_API_KEY)
previousTable = api.table(AIRTABLE_BASE_ID, "Previous")
client = genai.Client(api_key=GEMINI_API_KEY)
wp_client = Client(WORDPRESS_URL, WORDPRESS_USER, WORDPRESS_APP_PASSWORD)

# ===== ROUTES =====
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    topic = request.form.get("topic", "").strip()
    extra_context = request.form.get("extra_context", "").strip()
    files = request.files.getlist("images")

    # Upload to WP
    attachment_urls = []
    for file in files:
        data = {
            'name': file.filename,
            'type': file.content_type,
            'bits': xmlrpc_client.Binary(file.read())
        }
        response = wp_client.call(media.UploadFile(data))
        attachment_urls.append(response['url'])

    numfiles = len(attachment_urls)

    # Fetch Airtable Data
    tables = ["Preferences", "Keywords", "Context", "Previous"]
    pref, keyw, ctxt, prev = [], [], [], []
    for table_name in tables:
        records = api.table(AIRTABLE_BASE_ID, table_name).all()
        for record in records:
            if table_name == "Preferences":
                pref.append(record['fields'][table_name])
            elif table_name == "Keywords":
                keyw.append(record['fields'][table_name])
            elif table_name == "Context":
                ctxt.append(record['fields'][table_name])
            elif table_name == "Previous":
                prev.append(record['fields'][table_name])

    # Generate Titles
    extra_topic = f" Make sure each title contains the main idea of topic '{topic}'. NOTE: DO NOT INCLUDE ALL SMALL DETAILS only main points. Max length for title is 15 words" if topic else ""
    prompt = f"""
    Generate 10 possible blog titles, given the following parameters:
    Preferences: {pref}
    Keywords: {keyw}
    Context: {ctxt}
    Additional Context/Events: {extra_context}
    Do NOT reuse these previous blog titles: {prev}.
    {extra_topic}
    NOTE: THE OUTPUT MUST BE in the format: title 1, title 2, ... title 10 (no commas inside titles)
    """
    response = client.models.generate_content(model="gemini-2.5-flash",
                                              contents=prompt)
    titles = (response.text).split(", ")

    return render_template("choice.html",
                           titles=titles,
                           urls="|".join(attachment_urls),
                           extra_context=extra_context)


@app.route("/finalize", methods=["POST"])
def finalize():
    chosen = request.form["chosen"]
    extra_context = request.form.get("extra_context", "").strip()
    attachment_urls = request.form["urls"].split("|")
    numfiles = len(attachment_urls)

    previousTable.create({"Previous": chosen})

    tables = ["Preferences", "Keywords", "Context", "Previous"]
    pref, keyw, ctxt, prev = [], [], [], []
    for table_name in tables:
        records = api.table(AIRTABLE_BASE_ID, table_name).all()
        for record in records:
            fields = record.get('fields', {})

            if table_name == "Preferences":
                pref.append(record['fields'][table_name])
            elif table_name == "Keywords":
                keyword = fields.get("Keyword", "")
                link = fields.get("Link", "")
                keyw.append({"keyword": keyword, "link": link})
            elif table_name == "Context":
                ctxt.append(record['fields'][table_name])
            elif table_name == "Previous":
                prev.append(record['fields'][table_name])

    prompt = f"""
    Write a blog post with title {chosen}, it should include an intro, body (with headings), and conclusion.

    Preferences: {pref}
    Keywords: {keyw} (use the links provided in the keywords, and make sure to embed them in the blog post)
    Context: {ctxt}
    Additional Context/Events: {extra_context}
    Note you need to include and add in EXACTLY {numfiles} images

    Do NOT reuse these previous blog titles: {prev}

    NOTE: YOUR OUTPUT MUST BE IN HTML FORMAT, an example is shown below:
    Do not include the title at the start

    <p>Welcome to this sample blog post. Below are examples of how to include images using the tag with working URLs.</p><br><h2>Example 1: Displaying a Cat Image</h2><br><p>Here's an image of a cat:</p><br><IMAGEHERE/><br><h2>Example 2: Displaying a Dog Image</h2><br><p>Here's an image of a dog:</p><br><IMAGEHERE/><br><h2>Example 3: Displaying a Placeholder Image</h2><br><p>Here's a placeholder image:</p><br><IMAGEHERE/><br>

    as can be seen, the image locations are represented with the <IMAGEHERE>
    """
    response = client.models.generate_content(model="gemini-2.5-flash",
                                              contents=prompt)

    blog = response.text
    for i in range(numfiles):
        blog = blog.replace(
            "IMAGEHERE",
            f'''img src="{attachment_urls[i]}" alt="Uploaded Image" style="display: block; margin: 0 auto; width: 600px;"''',
            1)
        blog = blog.replace("h2", "h4")
        blog = blog.replace("h1", "h5")
        blog = blog.replace(f"<h5>{chosen}</h5>", "")

    post = WordPressPost()
    post.title = chosen
    post.content = blog
    post.post_status = 'draft'
    post_id = wp_client.call(NewPost(post))
    created_post = wp_client.call(GetPost(post_id))
    post_url = created_post.link
    wp_client.call(NewPost(post))

    return render_template("finalize.html", title=chosen, post_url=post_url)


if __name__ == "__main__":
    app.run(debug=True)