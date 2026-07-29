def get_prompt(company_name, sub_summary, content, target_word_count=75, min_word_count=70):

    '''
    For small documents, i.e., < 3 pages, the default value of target_word_count will be used
    For large documents, i.e., >= 3 pages, the default value of target_word_count will be used

    target_word_count / min_word_count let the caller widen the acceptable band on retries
    (e.g. 80/70, 85/70, ... up to 100/70) while never allowing the summary to drop below
    min_word_count regardless of how high target_word_count climbs.
    '''

    prompt = f"""Your task is to summarize the provided document specifically focusing on the company: {company_name}. Carefully adhere to the following instructions:

                    1. Purpose:

                    The summary must help investors understand significant developments related to the company.

                    2. Clarity and Length:

                    Write the summary using exactly {target_word_count} words. If exactly {target_word_count} words cannot be achieved while staying strictly accurate to the document, come as close to {target_word_count} as possible, but never use fewer than {min_word_count} words and never exceed {target_word_count} words.

                    Write the summary as a single paragraph without line breaks or extra spacing.

                    Don't mention the word count in the summary, and don't include any contact information.

                    3. Tone and Style:

                    Maintain a neutral, objective, and professional tone throughout the summary.

                    Do not include any salution in the summary.

					Do not address the summary to anyone.

                    4. Accuracy and Integrity:

                    Ensure that the summary contains only factual information explicitly mentioned in the original document.

                    Avoid adding interpretations, opinions, recommendations, advice or instructions.

                    Do not pad the summary with filler phrases, restated facts, or generic commentary just to reach the word count -- every added word must carry real information from the document.

                    5. Review and Verification:

                    After generating the summary, carefully verify its accuracy against the original document to ensure all included details are correct.

                    6. Document to Summarize: {sub_summary} {content}"""

    return prompt
