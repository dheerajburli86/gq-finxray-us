def get_prompt(old_summary, new_summary):

    prompt = f"""<INST>

                You are an expert in comparing text paragraphs for similarity of central message. Your task is to analyze two provided paragraphs and determine if they convey the same core information, even if there are minor differences in wording or details.

                Follow these instructions precisely:

                1.  **Analyze the Central Message:** Identify the primary point or main idea of each paragraph. What is each paragraph fundamentally about?

                2.  **Compare Core Information:** Determine if the central message of both paragraphs is the same. Consider whether a reader would gain the same understanding of the topic from either paragraph.

                3.  **Tolerate Minor Differences:** Acknowledge that the paragraphs may not be identical. Slight variations in wording, examples, or supporting details are acceptable, as long as the core message remains consistent.

                4.  **Focus on Similarity, Not Exact Match:** You are looking for *semantic* similarity (similarity in meaning), not *lexical* similarity (similarity in words).

                5.  **Output a JSON Object:** Your response *must* be a JSON object with a single key: `"is_similar"`.

                6.  **Boolean Value:** Set the value of `"is_similar"` to `"True"` if the two paragraphs convey the same central message. Set it to `"False"` if they do not.

                7.  **No Extraneous Text:** Do *not* include any other text in your response besides the JSON object. No explanations, justifications, or conversational elements are permitted.

                Here are the paragraphs to compare:

                Paragraph 1: {old_summary}

                Paragraph 2: {new_summary}
                
                </INST>"""
               
    return prompt