def get_prompt(company_name, content, cur_date):

    prompt = f"""<INST>
    
    Your task is to read the provided text and evaluate the perceived impact of the information on the future stock performance of {company_name}.  You must classify the impact as either HIGH, MEDIUM, or LOW.

    Follow these instructions precisely:

    1.  **Read and Understand:** Carefully read the text provided after this instruction block.
    2.  **Categorization:** Classify the text's impact on {company_name}'s stock into *one* of the following categories: HIGH, MEDIUM, or LOW.
    3.  **Exhaustive Rules:** Apply the following rules in order to determine the classification. Be exhaustive with your application of the rules.
        *   **Rule 1 (LOW):** If the text describes routine compliance matters where no significant findings are reported, classify it as LOW.
        *   **Rule 2 (LOW):** If the text relates to share certificates being lost, reissued, or similar administrative tasks, classify it as LOW.
        *   **Rule 3 (LOW):** If the text contains information that will have *no* discernible impact on the stock price, classify it as LOW. 
        *   **Rule 4 (LOW):** If any of the inputs are missing or improperly formed, classify it as LOW. 
        *   **Rule 5 (LOW):** If the text primarily talks about the closure of trading window and similar routine compliance matters, classify it as LOW. 
        *   **Rule 6 (LOW):** Assume today's date to be {cur_date}. If the text exclusively recounts *materially old* events (i.e., more than 1 month in the past) **without** any mention of a current development or potential future impact, classify it as LOW.
        *   **Rule 7 (MEDIUM):** If the text is about upcoming meetings (e.g., board meetings, shareholder meetings, earnings calls, analyst meetings, etc.), classify it as MEDIUM.
        *   **Rule 8 (HIGH):** If the text reports a change in Key Management Personnel (e.g., CEO, CFO, CTO, board members, etc.), classify it as HIGH.
        *   **Rule 9 (HIGH):** If the text discusses purchase orders received by {company_name}, classify it as HIGH.
        *   **Rule 10 (HIGH):** If the text describes acquisitions or disposals (buying or selling of assets, businesses, or significant stakes in other companies) involving {company_name}, classify it as HIGH.
        *   **Rule 11 (Discretion):** For all other matters not covered by the above rules, use your understanding of financial markets and business to determine the impact. Consider factors like the magnitude of the news, potential financial implications, and relevance to investor sentiment.

    4.  **Output Format:** Provide your answer in a JSON format with the key 'impact'. The value should be one of the following words: "HIGH", "MEDIUM", or "LOW". For example: `{{'impact': 'LOW'}}`.

    5. Please do not output any other text besides the JSON classification.
    
    6. **Text to be Evaluated:** {content}
    
    </INST>"""
               
    return prompt