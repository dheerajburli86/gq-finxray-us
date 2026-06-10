def get_prompt(company_name, sub_summary, content):

    prompt = f"""Your task is to summarize the provided news article specifically focusing on the company: {company_name}. Carefully adhere to the following instructions:

                    1. Content Scope:

                    Include only those details from the article that are directly relevant to {company_name}.

                    Completely exclude any information unrelated to {company_name}.

                    2. Purpose:

                    The summary must help investors understand significant developments related to the company.

                    3. Clarity and Length:

                    Keep the summary concise, clear, and strictly under 75 words.

                    Write the summary as a single paragraph without line breaks or extra spacing.
                    
                    Don't mention the word count in the summary, and don't include any contact information.

                    4. Tone and Style:

                    Maintain a neutral, objective, and professional tone throughout the summary.
                    
                    Do not include any salution in the summary.

					Do not address the summary to anyone.

                    5. Accuracy and Integrity:

                    Ensure that the summary contains only factual information explicitly mentioned in the original article.

                    Avoid adding interpretations, opinions, recommendations, advice or instructions.

                    6. Review and Verification:

                    After generating the summary, carefully verify its accuracy against the original article to ensure all included details are correct.

                    7. Inclusiveness:

                    Ensure that the summary is inclusive and does not exclude any important information from the original article. If the name of the person or organisation, sharing the opinion, is mentioned in the article then include it in the summary else ignore this instruction.

                    8. News Article to Summarize: {sub_summary} {content}"""
               
    return prompt