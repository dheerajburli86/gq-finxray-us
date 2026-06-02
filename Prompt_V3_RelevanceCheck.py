def get_prompt(company_name, summary):

    prompt = f"""
            <INST>
            
            Your task is to carefully read the provided paragraph of text and determine if the text is relevant to the company: {company_name}
            
            Text to evaluate: {summary}
            
            Return a JSON object with a single boolean key 'is_relevant' set to "True" if the provided text is relevant to the specified company, else set 'is_relevant' to "False".
    
            Please do not output any other text besides the JSON classification.
            
            </INST>"""
               
    return prompt