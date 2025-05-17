import streamlit as st
import json
import pandas as pd
import re
import time
from PyPDF2 import PdfReader
from openai import OpenAI
from sklearn.metrics.pairwise import cosine_similarity as sk_cosine
import joblib
import numpy as np

# === Set up client ===
st.set_page_config(page_title="StepUpYourCareer.AI", layout="wide")
client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])


if 'page' not in st.session_state:
    st.session_state.page = 1
if 'name' not in st.session_state:
    st.session_state.name = ""
if 'email' not in st.session_state:
    st.session_state.email = ""

def page_1():
    st.title("Welcome to StepUpYourCareer.AI")
    st.markdown("##### Let's get started with a few details.")

    name = st.text_input("Your Full Name")
    email = st.text_input("Your Email Address")

    if name and email:
        if st.button("➡️ Proceed to Resume Analysis"):
            st.session_state.name = name
            st.session_state.email = email
            st.session_state.page = 2

def load_examples():
            with open("/mount/src/stepupyourcareer.ai/StepUpAI/skill_gap_analysis.json", "r") as f:
                examples = json.load(f)
            example_texts = [
                f"Resume: {ex['resume_summary']} | Role: {ex['target_role']}" for ex in examples
            ]
            embeddings = [get_embedding(text) for text in example_texts]
            return examples, embeddings

# === Load Role Skills ===
@st.cache_data
def load_role_skills():
    with open("/mount/src/stepupyourcareer.ai/StepUpAI/role_skills.json", "r") as f:
        role_skills_list = json.load(f)
        return {
            entry["role"]: {
                "technical_skills": entry.get("technical_skills", []),
                "soft_skills": entry.get("soft_skills", [])
                }
            for entry in role_skills_list if "role" in entry
            }

# Embedding
def get_embedding(text, model="text-embedding-ada-002"):
    response = client.embeddings.create(input=[text], model=model)
    return response.data[0].embedding

        # Resume text extraction ===
def extract_text_from_pdf(uploaded_file):
    reader = PdfReader(uploaded_file)
    return "\n".join([page.extract_text() or "" for page in reader.pages])

# Resume anonymizer
def anonymize(text):
    response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {"role": "system", "content": "You are a helpful assistant that removes personal identifiers from resumes."},
        {"role": "user", "content": f"Anonymize this resume:\n\n{text}"}
        ],
        temperature=0
        )
    return response.choices[0].message.content.strip()

# RAG-based retrieval 
def retrieve_examples(query, embeddings, examples, k=3):
    query_emb = get_embedding(query)
    sims = sk_cosine([query_emb], embeddings)[0]
    top_k = sims.argsort()[-k:][::-1]
    return [examples[i] for i in top_k]

# Skill gap analysis 
def generate_skill_gap(resume_text, target_role, retrieved_examples, fallback_skills):
    examples_prompt = "\n\n".join([
    f"Example for role {ex['target_role']}:\nResume: {ex['resume_summary']}\nSkill Gaps: tech={ex['technical_skill_gap']}, soft={ex['soft_skill_gap']}, transferable={ex['transferable_skills']}"
    for ex in retrieved_examples
        ])
    expected_tech = fallback_skills.get("technical_skills", [])
    expected_soft = fallback_skills.get("soft_skills", [])

    prompt = f"""
        You are a highly experienced career advisor. Based on examples and required skills, identify skill gaps.

        ## EXAMPLES
        {examples_prompt}

        ## TASK:

        Analyze the new resume below and return only JSON:
        {{
        "technical_skill_gaps": [list of missing technical skills],
        "soft_skill_gaps": [list of missing soft skills],
        "transferable_skills": [skills from the resume that help bridge gaps]
        }}

        No explanation, no markdown, just clean JSON.

        Target Role: {target_role}
        Required Technical Skills: {', '.join(expected_tech)}
        Required Soft Skills: {', '.join(expected_soft)}
        Resume:
        {resume_text}
        """

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "You are a helpful assistant for skill gap analysis."},
        {"role": "user", "content": prompt}],
        temperature=0.0
        )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"```json|```", "", raw)
    return json.loads(raw)

def get_skill_priorities_from_gpt(skills, role):
    prompt = f"""
    You are a career advisor. For the target role '{role}', prioritize the following skills based on the 80/20 (Pareto) principle.

    Skills:
    {', '.join(skills)}

    Return JSON mapping each skill to an importance score from 1 to 100 (higher means more important). Output should look like:
    {{
      "Skill1": 95,
      "Skill2": 90,
      ...
    }}

    No explanation. No markdown. Only JSON.
    """
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that ranks skills by importance."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.2
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"```json|```", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


# Action Plan Generator
def extract_json_from_response(raw):
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return {}
    return {}

def load_skill_resources():
    with open("/mount/src/stepupyourcareer.ai/StepUpAI/skill_resource_mapping.json", "r") as f:
        return json.load(f)

# Helper: Split skill lists into (in-RAG, out-of-RAG) 
def split_skills_by_rag_presence(skills, rag_skill_keys):
    present = []
    missing = []
    for skill in skills:
        if skill.strip().upper() in rag_skill_keys:
            present.append(skill)
        else:
            missing.append(skill)
    return present, missing

def generate_hybrid_action_plan(tech, soft, trans, skill_resources):
    # Split all three skill types
    tech_in, tech_out = split_skills_by_rag_presence(tech, skill_resources.keys())
    soft_in, soft_out = split_skills_by_rag_presence(soft, skill_resources.keys())
    trans_in, trans_out = split_skills_by_rag_presence(trans, skill_resources.keys())

    #  Construct RAG results
    def extract_rag(skills):
        result = {}
        for s in skills:
            key = s.strip().upper()
            if key in skill_resources:
                    result[s] = skill_resources[key]
        return result

    plan = {
            "message": "Here's a complete roadmap with relevant resources",
            "technical_skill_resources": extract_rag(tech_in),
            "soft_skill_resources": extract_rag(soft_in),
            "transferable_skill_resources": extract_rag(trans_in)
    }

    # Prepare GPT prompt only for uncovered skills
    if tech_out or soft_out or trans_out:
        prompt = f"""
        You are a career coach. Only generate resources for the following skills not found in our internal library.

        Provide for each:
        - One **top-rated course** with real working URL.
        - One **real book** just name & author and AMAZON links for buying that book.
        - For soft/transferable skills, one article or video (with URL).

        Format your response in JSON like this:
        {{
        "technical_skill_resources": {{
            "Skill": [{{"title": "...", "url": "..." }}, {{"title": "Book by Author"}}]
        }},
        "soft_skill_resources": {{
            "Skill": [{{"title": "...", "url": "..." }}]
        }},
        "transferable_skill_resources": {{
            "Skill": [{{"title": "...", "url": "..." }}]
        }}
        }}

        Only cover these:
        - TECHNICAL: {', '.join(tech_out)}
        - SOFT: {', '.join(soft_out)}
        - TRANSFERABLE: {', '.join(trans_out)}

        You will be penalized if you confabulate or hallucinate by creating fake resources. It should be 100% authenthic.
        Double check every link/resource you give. If you don't get any links leave that section blank.
        No explanations. No markdown. Only JSON.
        """
        try:
            response = client.chat.completions.create(
            # Change to GPT 4 to get accurate links to resources
            model="gpt-4o-mini",
            messages=[
                    {"role": "system", "content": "You are a helpful assistant for learning."},
                    {"role": "user", "content": prompt}
                    ],
                    temperature=0.2
                    )
            raw = response.choices[0].message.content.strip()
            raw = re.sub(r"```json|```", "", raw)
            gpt_part = extract_json_from_response(raw)

            if not isinstance(gpt_part, dict):
                gpt_part = {}

            # Merge GPT results into RAG base
            for k in ["technical_skill_resources", "soft_skill_resources", "transferable_skill_resources"]:
                if k in gpt_part and isinstance(plan.get(k), dict):
                    plan[k].update(gpt_part[k])


        except Exception as e:
                st.error(f"Error generating GPT fallback plan: {e}")

        return plan
        
# Load Mentor Clustering Model & Vectorizer
with open('/mount/src/stepupyourcareer.ai/StepUpAI/models/mentor_clustering_model.pkl', 'rb') as f:
        kmeans_final = joblib.load(f)
with open('/mount/src/stepupyourcareer.ai/StepUpAI/models/fitted_vectorizer.pkl', 'rb') as f:
    vectorizer = joblib.load(f)
mentors_final_data = pd.read_json("/mount/src/stepupyourcareer.ai/StepUpAI/mentors_final_data.json")

def page_2():
    st.title("📄 Resume Analyzer + Mentor Recommender")
    st.markdown(f"**👤 Name:** {st.session_state.name}  |  **📧 Email:** {st.session_state.email}")

    st.markdown("Please ensure your resume reflects your true skills and experiences — being honest helps us generate the most accurate and helpful guidance for your growth.")

    uploaded_file = st.file_uploader("📄 Upload your resume (PDF)", type=["pdf"])
    target_role = st.selectbox("🎯 Select your target role", [
        "AI Engineer", "Data Analyst", "Business Analyst",
        "Business Intelligence Analyst", "Machine Learning"
    ])
    if st.button("🔍 Analyze Resume") and uploaded_file and target_role:
        st.info("Processing resume... Please wait.")

        resume_text = extract_text_from_pdf(uploaded_file)
        anonymized_resume = anonymize(resume_text)

        examples, example_embeddings = load_examples()
        skills_dict = load_role_skills()
        fallback_skills = skills_dict.get(target_role, {})
        query = f"Resume: {anonymized_resume} | Role: {target_role}"
        retrieved = retrieve_examples(query, example_embeddings, examples)

        gaps = generate_skill_gap(anonymized_resume, target_role, retrieved, fallback_skills)

        all_skills = list(set(gaps["technical_skill_gaps"] + gaps["soft_skill_gaps"] + gaps["transferable_skills"]))
        skill_priorities = get_skill_priorities_from_gpt(all_skills, target_role)

       

        # Sort gaps by importance
        gaps["technical_skill_gaps"] = sorted(gaps["technical_skill_gaps"], key=lambda x: -skill_priorities.get(x, 0))
        gaps["soft_skill_gaps"] = sorted(gaps["soft_skill_gaps"], key=lambda x: -skill_priorities.get(x, 0))
        gaps["transferable_skills"] = sorted(gaps["transferable_skills"], key=lambda x: -skill_priorities.get(x, 0))

        st.success("Analysis complete!")

        st.subheader("🔍 Skill Gap Summary")
        gap_cols_html = """
        <div style='display: flex; gap: 20px;'>
            <div style='flex: 1; padding: 20px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); background-color: #1e1e1e;'>
                <h4 style='color:white;'>Technical Skill Gaps</h4>
                <ul style='color:white;'>{}</ul>
            </div>
            <div style='flex: 1; padding: 20px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); background-color: #1e1e1e;'>
                <h4 style='color:white;'>Soft Skill Gaps</h4>
                <ul style='color:white;'>{}</ul>
            </div>
            <div style='flex: 1; padding: 20px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); background-color: #1e1e1e;'>
                <h4 style='color:white;'>Transferable Skills</h4>
                <ul style='color:white;'>{}</ul>
            </div>
        </div>
        """
        st.markdown(gap_cols_html.format(
            "".join([f"<li>{s}</li>" for s in gaps["technical_skill_gaps"]]),
            "".join([f"<li>{s}</li>" for s in gaps["soft_skill_gaps"]]),
            "".join([f"<li>{s}</li>" for s in gaps["transferable_skills"]])
        ), unsafe_allow_html=True)

        st.subheader("📘 Personalized Action Plan")
        tech = gaps.get("technical_skill_gaps", [])
        soft = gaps.get("soft_skill_gaps", [])
        trans = gaps.get("transferable_skills", [])

         # Add explanation about Pareto Principle
        st.markdown("""
        ### 🎯 Focus Using the Pareto Principle (80/20 Rule)
        According to the Pareto Principle, 20% of the skills often lead to 80% of the results.
        Based on your resume and the target role, we recommend focusing on the top 3 most impactful skills first:
        """)

        # Merge and sort all gaps with priority scores
        all_gap_skills = tech + soft + trans
        ranked_skills = sorted(all_gap_skills, key=lambda s: -skill_priorities.get(s, 0))

        # Take top 3 distinct skill names
        top_skills = []
        for s in ranked_skills:
            if s not in top_skills:
                top_skills.append(s)
            if len(top_skills) == 3:
                break

        # Show top 3
        for i, skill in enumerate(top_skills, 1):
            st.markdown(f"**{i}. {skill}** – Priority Score: {skill_priorities.get(skill, 'N/A')}")
            
        skill_resources = load_skill_resources()
        plan = generate_hybrid_action_plan(tech, soft, trans, skill_resources)

        st.info(plan.get("message", "Here are some great learning resources!"))
        def render_resources_flex(title, resources):
            if resources:
                st.markdown(f"<h4 style='margin-top:30px;'>{title}</h4>", unsafe_allow_html=True)
                box_html = "<div style='display: flex; flex-wrap: wrap; gap: 20px;'>"
                for skill, items in resources.items():
                    item_html = f"<div style='flex: 1 1 calc(33% - 20px); min-width: 300px; padding: 20px; border-radius: 12px; background-color: #1e1e1e; box-shadow: 0 4px 12px rgba(0,0,0,0.25); color: white;'>"
                    item_html += f"<h5 style='font-size: 1.2rem; margin-bottom: 10px;'>{skill}</h5><ul style='margin-top: 10px;'>"
                    for item in items:
                        if isinstance(item, dict):
                            title = item.get("title", "")
                            url = item.get("url", "")
                            if url:
                                item_html += f"<li><a href='{url}' target='_blank' style='color: #1e90ff;'>{title}</a></li>"
                            else:
                                item_html += f"<li>{title}</li>"
                        elif isinstance(item, str):
                            item_html += f"<li>{item}</li>"
                    item_html += "</ul></div>"
                    box_html += item_html
                box_html += "</div>"
                st.markdown(box_html, unsafe_allow_html=True)

        render_resources_flex("🛠️ Technical Skills", plan.get("technical_skill_resources", {}))
        render_resources_flex("💬 Soft Skills", plan.get("soft_skill_resources", {}))
        render_resources_flex("🔄 Transferable Skills", plan.get("transferable_skill_resources", {}))

        st.subheader("👥 Recommended Mentors")
        try:
            skill_list = [s.strip() for s in tech if s.strip()]
            mlb_vector = vectorizer.transform([skill_list])
            cluster_id = kmeans_final.predict(mlb_vector)[0]
            mentors = mentors_final_data[mentors_final_data["cluster"] == cluster_id]

            if not mentors.empty:
                rows = [mentors.iloc[i:i+3] for i in range(0, len(mentors), 3)]
                for row in rows:
                    cols = st.columns(3)
                    for idx, (_, mentor) in enumerate(row.iterrows()):
                        with cols[idx]:
                            st.markdown(
                                f"""
                                <div style='
                                    background-color: #1e1e1e;
                                    padding: 20px;
                                    border-radius: 12px;
                                    margin-bottom: 20px;
                                    height: 280px;
                                    display: flex;
                                    flex-direction: column;
                                    justify-content: space-between;
                                    box-shadow: 0 4px 12px rgba(0,0,0,0.25);
                                '>
                                    <div>
                                        <h4 style='margin-bottom: 5px;'>{mentor['name']}</h4>
                                        <p style='margin: 0 0 8px 0; font-size: 0.9em;'>
                                            <a href='https://www.linkedin.com/in/{mentor['linkedin_id']}' target='_blank' style='color: #1e90ff;'>🔗 LinkedIn</a>
                                        </p>
                                        <p><strong>Skills:</strong> {', '.join(mentor['technical_skills'])}</p>
                                    </div>
                                    <p style='font-size: 0.9em; color: #ccc; margin-top: 10px;'>{mentor['bio']}</p>
                                </div>
                                """,
                                unsafe_allow_html=True
                            )
            else:
                st.warning("No matching mentors found.")
        except Exception as e:
            st.error(f"Mentor recommendation error: {e}")

        st.session_state.result = {
            "target_role": target_role,
            "resume_text": anonymized_resume,
            **gaps,
            "action_plan": plan,
            "mentors": mentors.to_dict(orient="records") if not mentors.empty else []   
        }
    

    def format_result_as_html(result, user_name):
            html = f"<h2>Hi {user_name},</h2>"
            html += f"<p>Here is your personalized skill gap analysis for the role of <strong>{result['target_role']}</strong>.</p>"

            def to_list_html(title, items):
                return f"<h3>{title}</h3><ul>{''.join(f'<li>{i}</li>' for i in items)}</ul>" if items else ""

            html += to_list_html("Technical Skill Gaps", result.get("technical_skill_gaps", []))
            html += to_list_html("Soft Skill Gaps", result.get("soft_skill_gaps", []))
            html += to_list_html("Transferable Skills", result.get("transferable_skills", []))

            html += "<h3>Recommended Resources</h3>"
            for key, label in [
                ("technical_skill_resources", "Technical Skills"),
                ("soft_skill_resources", "Soft Skills"),
                ("transferable_skill_resources", "Transferable Skills")
            ]:
                html += f"<h4>{label}</h4><ul>"
                for skill, resources in result["action_plan"].get(key, {}).items():
                    html += f"<li><strong>{skill}</strong><ul>"
                    for item in resources:
                        if isinstance(item, dict):
                            title = item.get("title", "")
                            url = item.get("url", "")
                            html += f"<li><a href='{url}' target='_blank'>{title}</a></li>" if url else f"<li>{title}</li>"
                        elif isinstance(item, str):
                            html += f"<li>{item}</li>"
                    html += "</ul></li>"
                html += "</ul>"
            
            # Add Recommended Mentors to Email
            html += "<h3>👥 Recommended Mentors</h3>"
            mentors = result.get("mentors", [])
            if mentors:
                html += "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse: collapse;'>"
                html += "<tr><th>Name</th><th>Skills</th><th>Bio</th><th>LinkedIn</th></tr>"
                for m in mentors:
                    name = m.get("name", "N/A")
                    skills = ", ".join(m.get("technical_skills", []))
                    bio = m.get("bio", "N/A")
                    linkedin = m.get("linkedin_id", "")
                    html += f"""
                    <tr>
                        <td>{name}</td>
                        <td>{skills}</td>
                        <td>{bio}</td>
                        <td><a href="https://www.linkedin.com/in/{linkedin}" target="_blank">🔗</a></td>
                    </tr>
                    """
                html += "</table>"
            else:
                html += "<p>No mentor matches found.</p>"

            html += "<p>Best of luck!<br/>– StepUpYourCareer.AI</p>"
            return html
        
    def send_email(to_email, subject, html_content):
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText
            import smtplib

            from_email = st.secrets["EMAIL_USERNAME"]
            password = st.secrets["EMAIL_PASSWORD"]

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = from_email
            msg["To"] = to_email

            part = MIMEText(html_content, "html")
            msg.attach(part)

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(from_email, password)
                server.sendmail(from_email, to_email, msg.as_string())

        
    if "result" in st.session_state:
        if st.button("📧 Email Me the Results"):
            try:
                email_html = format_result_as_html(st.session_state.result, st.session_state.name)
                send_email(st.session_state.email, f"Your Skill Gap Report for {st.session_state.result['target_role']}", email_html)
                st.success("Email sent successfully!")
            except Exception as e:
                st.error(f"Failed to send email: {e}")



# === Page Router ===
if st.session_state.page == 1:
    page_1()
elif st.session_state.page == 2:
    page_2()
