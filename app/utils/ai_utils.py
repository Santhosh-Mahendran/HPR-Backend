from openai import OpenAI
import os


# Initialize OpenRouter client
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key= os.environ.get('AI_API_KEY'),
)

def ask_openrouter(question, context):
    full_prompt = f"""You are a helpful assistant. Use the context below to answer the user's question.

Book content:
{context}

Question: {question}

Answer:"""

    try:
        # Make the API call
        completion = client.chat.completions.create(
            extra_headers={
                "HTTP-Referer": "<YOUR_SITE_URL>",  # Optional
                "X-Title": "<YOUR_SITE_NAME>",      # Optional
            },
            extra_body={},
            model="meta-llama/llama-3.3-70b-instruct:free",
            messages=[
                {"role": "user", "content": full_prompt}
            ]
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        return f"⚠️ OpenRouter Error: {e}"