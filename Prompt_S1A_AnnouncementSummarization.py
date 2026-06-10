def get_prompt(company_name, sub_summary, content, target_word_count=75):
    
    '''
    For small documents, i.e., < 3 pages, the default value of target_word_count will be used
    For large documents, i.e., >= 3 pages, the default value of target_word_count will be used
    '''

    prompt = f"""Your task is to summarize the provided document specifically focusing on the company: {company_name}. Carefully adhere to the following instructions:

                    1. Purpose:

                    The summary must help investors understand significant developments related to the company.

                    2. Clarity and Length:

                    Keep the summary concise, clear, and strictly under {target_word_count} words.

                    Write the summary as a single paragraph without line breaks or extra spacing.
                    
                    Don't mention the word count in the summary, and don't include any contact information.

                    3. Tone and Style:

                    Maintain a neutral, objective, and professional tone throughout the summary.
                    
                    Do not include any salution in the summary.

					Do not address the summary to anyone.

                    4. Accuracy and Integrity:

                    Ensure that the summary contains only factual information explicitly mentioned in the original document.

                    Avoid adding interpretations, opinions, recommendations, advice or instructions.

                    5. Review and Verification:

                    After generating the summary, carefully verify its accuracy against the original document to ensure all included details are correct.

                    6. Document to Summarize: {sub_summary} {content}"""
               
    return prompt