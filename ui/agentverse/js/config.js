/**
 * Configuration and Constants
 */

export const CONFIG = {
  DEFAULT_MAX_ITERATIONS: 3,
  REQUEST_TIMEOUT_MS: 300000, // 5 minutes
  TRUNCATE_LENGTH: 300,
  TIMER_UPDATE_INTERVAL_MS: 100,
};

export const EXAMPLE_TASKS = {
  math: `A school is designing a small amusement park with three rides. The first ride costs $500 to build and makes $20 per ride. The second ride costs $700 and makes $25 per ride. The third ride costs $900 and makes $30 per ride.

The school has a budget of $2000 and wants to maximize revenue in 1 day if 100 students ride each ride at most once.

Determine which rides to build and calculate the expected total revenue.`,
  
  research: `Research the current state of renewable energy adoption globally. Focus on:
1. Leading countries in solar and wind energy
2. Key technological advancements in the past 3 years
3. Economic factors driving adoption
4. Challenges and barriers to wider adoption

Provide a comprehensive summary with key findings and recommendations for a company looking to invest in this sector.`,
  
  consulting: `A mid-sized e-commerce company wants to implement AI-powered customer service to reduce support costs and improve response times. They currently handle 10,000 support tickets per month with a team of 15 agents.

Provide recommendations considering:
- Available AI/chatbot solutions
- Expected cost savings vs implementation costs
- Impact on customer experience
- Implementation timeline and risks
- Training requirements for existing staff`,
  
  coding: `Design and implement a task management system with the following features:
1. Create, read, update, and delete tasks
2. Assign priority levels (low, medium, high, urgent)
3. Set due dates and track overdue tasks
4. Filter and sort tasks by various criteria
5. Simple command-line interface

Provide the Python code with proper error handling and documentation.`
};
