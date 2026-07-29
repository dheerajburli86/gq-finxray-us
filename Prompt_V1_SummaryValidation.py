def get_prompt(content):

    prompt = f"""<INST> Task: Analyze the provided paragraph for potential issues and generate a JSON output.

            Follow these instructions exclusively:

            A. Violation Rules: For a paragraph to be identified as having a potential issue, it must explicitly violate at least one of the four rules below:

            1.  **Incomplete Sentence Check:** Identify any grammatically or logically incomplete sentences. An incomplete sentence lacks a subject, verb, or expresses an unfinished thought.

            2.  **Dummy Value Detection:** Identify if the paragraph contains dummy values such as X instead of actual numerical values.This check should only flag dummy values reported in the financial or performance context and ignore all other contexts like dates, phone number etc.

            3.  **Word Count Reference Check:** Identify any explicit references to the word count of the paragraph (e.g., "This summary contains X words," "The word count is...", "within the Y-word limit").

            4.  **Instruction Detection:** Identify any language where the reader is explicitly the subject of a verb.

            5. **First Person Narative Detection:** Identify if the paragraph contains the first person narrative (e.g., "I am...", "I have...", "I will...", "I can...") from the perspective of the paragraph issuer. Do not flag if it is clear that a person is being quoted in the paragraph.

            B. Response Rules: Please structure your response as per the following rules:

            1.  **JSON Output:** Generate a JSON object based on your analysis:

                *   Note that multiple issues could exist in the paragraph. If *any* of the above issues are detected, return: {{"issues_detected": "True"}}.
                *   If *none* of the above issues are detected, return: {{"issues_detected": "False"}}

            2.  **Corrected Summary (Conditional):** Note that multiple issues could exist in the paragraph. If *any* issues are detected, create a corrected summary by removing all the problematic parts. Return this corrected summary under the key "corrected_summary" in the JSON response. If no issues are detected, *do not* include the "corrected_summary" key.

            3.  **Output Format:** Your response *must* be a JSON object.

            *   **If issues are detected:** {{"issues_detected": "True", "corrected_summary": "[corrected summary text]"}}. The "corrected_summary" value should be the paragraph with problematic parts removed.
            *   **If no issues are detected:** {{"issues_detected": "False"}}

            4. Do not include any additional text, explanations, or conversational elements.  The output should consist solely of the JSON object.

            5. Do not add any double quotes in the JSON response.

            **Paragraph to be Analyzed:** {content}

            </INST>"""

    return prompt
