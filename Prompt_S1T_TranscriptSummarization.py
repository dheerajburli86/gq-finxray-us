def get_prompt(company_name, sub_summary, content, target_word_count=75, min_word_count=70):

    '''
    Third summary class alongside S.1.N (news) and S.1.A (announcements):
    S.1.T is for earnings call transcripts (Feature 11).

    Same retry contract as S.1.A -- target_word_count/min_word_count widen on
    retries (75/70, 80/70, 85/70, ... up to 100/70) via the exact same
    escalation ladder in ai_pipeline.py. Kept as its own prompt rather than
    reusing S.1.A because a transcript is a conversation (prepared remarks +
    analyst Q&A), not a filed document -- the summary needs to extract
    management's stated numbers/guidance and tone, not just "summarize the
    document."
    '''

    prompt = f"""Your task is to summarize the provided earnings call transcript specifically focusing on the company: {company_name}. Carefully adhere to the following instructions:

                    1. Content Scope:

                    Focus on what management said about financial results, guidance, and strategy during the call, plus any notable analyst questions and the answers given during Q&A.

                    Prioritize concrete numbers (revenue, margins, guidance figures) and forward-looking statements over generic commentary about the business.

                    2. Purpose:

                    The summary must help investors understand what management actually said on the call that could move the stock -- results, guidance changes, tone shifts, or notable analyst pushback.

                    3. Clarity and Length:

                    Write the summary using exactly {target_word_count} words. If exactly {target_word_count} words cannot be achieved while staying strictly accurate to the transcript, come as close to {target_word_count} as possible, but never use fewer than {min_word_count} words and never exceed {target_word_count} words.

                    Write the summary as a single paragraph without line breaks or extra spacing.

                    Don't mention the word count in the summary, and don't include any contact information.

                    4. Tone and Style:

                    Maintain a neutral, objective, and professional tone throughout the summary.

                    Do not include any salutation in the summary.

                    Do not address the summary to anyone.

                    Attribute statements to "management" or "the CEO/CFO" only if the transcript text clearly identifies the speaker's role; otherwise attribute generically to "management."

                    5. Accuracy and Integrity:

                    Ensure that the summary contains only factual information explicitly stated on the call.

                    Avoid adding interpretations, opinions, recommendations, advice, or instructions not attributable to a speaker on the transcript.

                    Do not pad the summary with filler phrases, restated facts, or generic commentary just to reach the word count -- every added word must carry real information from the call.

                    6. Review and Verification:

                    After generating the summary, carefully verify its accuracy against the original transcript to ensure all included details and figures are correct.

                    7. Earnings Call Transcript to Summarize: {sub_summary} {content}"""

    return prompt
