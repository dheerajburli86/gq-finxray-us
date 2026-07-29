def get_prompt(text):

    prompt = f"""
            <INST>

            Analyze the text below. If it is gibberish, respond with {{"is_gibberish":true}}. If it is meaningful, respond with {{"is_gibberish":false}}. Ensure your response strictly adheres to this JSON format.

            Text: {text}
            </INST>"""

    return prompt
