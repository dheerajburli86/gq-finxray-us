def get_prompt(company_name, content, cur_date):

    prompt = f"""<INST>

    Your task is to read the provided text and evaluate the perceived impact of the information on the future stock performance of {company_name}. You must classify the impact as either HIGH, MEDIUM, or LOW.

    Follow these instructions precisely:

    1. **Read and Understand:** Carefully read the text provided after this instruction block.
    2. **Categorization:** Classify the text's impact on {company_name}'s stock into *one* of the following categories: HIGH, MEDIUM, or LOW.
    3. **Exhaustive Rules:** Apply the following rules in order to determine the classification.

        **HIGH Rules — classify as HIGH if any of these apply:**
        *   Rule H1: Change in Key Management Personnel — CEO, CFO, CTO, COO, President, or board member appointment, resignation, or termination.
        *   Rule H2: Earnings results — quarterly or annual revenue, net income, EPS reported. Especially if beating or missing analyst estimates significantly.
        *   Rule H3: Acquisition, merger, or disposal — company buying or selling another business, significant asset, or major stake.
        *   Rule H4: Major contract or purchase order — significant new customer win, government contract, or partnership agreement.
        *   Rule H5: Bankruptcy, insolvency, restructuring, or Chapter 11 filing.
        *   Rule H6: FDA approval, clinical trial results, or major regulatory decision affecting the company.
        *   Rule H7: Significant insider buying — executive or director purchasing company stock with personal money. Especially purchases over $500,000 total value.
        *   Rule H8: SEC investigation, fraud allegation, or major legal judgment against the company.
        *   Rule H9: Stock buyback program announcement or major dividend change.
        *   Rule H10: IPO filing (S-1) or major capital raise.
        *   Rule H11: Delisting notice or exchange transfer.
        *   Rule H12: Major product launch, breakthrough technology announcement, or significant market expansion.

        **MEDIUM Rules — classify as MEDIUM if any of these apply:**
        *   Rule M1: Upcoming earnings call, board meeting, shareholder meeting, or analyst day announcement.
        *   Rule M2: Insider selling of company stock — executive or director selling shares. Any amount.
        *   Rule M3: Insider buying under $500,000 total value.
        *   Rule M4: Minor contract win or renewal.
        *   Rule M5: Analyst rating change — upgrade, downgrade, or price target revision.
        *   Rule M6: Guidance update — company revising its financial outlook up or down.
        *   Rule M7: Strategic partnership or joint venture announcement.
        *   Rule M8: Dividend declaration (regular quarterly dividend).
        *   Rule M9: Share issuance, secondary offering, or dilution event.
        *   Rule M10: Management commentary on market conditions or business outlook.
        *   Rule M11: Clinical trial update or drug pipeline progress (not final approval).

        **LOW Rules — classify as LOW if none of the above apply:**
        *   Rule L1: Routine compliance filing with no significant findings.
        *   Rule L2: Lost share certificates or administrative tasks.
        *   Rule L3: Trading window closure notification.
        *   Rule L4: Events more than 1 month old with no current relevance.
        *   Rule L5: General market commentary with no specific company impact.
        *   Rule L6: Missing or improperly formed input.

    4. **Important:** When in doubt between HIGH and MEDIUM, choose HIGH. When in doubt between MEDIUM and LOW, choose MEDIUM. Err on the side of higher impact — it is better to alert investors about something moderately important than to miss something significant.

    5. **Output Format:** Provide your answer in JSON format with the key 'impact'. The value must be one of: "HIGH", "MEDIUM", or "LOW". Example: {{'impact': 'HIGH'}}

    6. Do not output any other text besides the JSON classification.

    7. **Text to be Evaluated:** {content}

    </INST>"""

    return prompt
