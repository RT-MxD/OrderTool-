from typing import TypedDict, List, Dict, Any
from langgraph.graph import StateGraph, END
import pandas as pd

# Define the state for our LangGraph workflow
class GraphState(TypedDict):
    input_data: List[Dict[str, Any]]
    processed_data: pd.DataFrame

def process_node(state: GraphState):
    """
    This node processes the input data:
    1. Extracts specific columns (Design, Beam, Feeders, etc.)
    2. Divides the Piece value by 3
    3. Groups and sorts the data by Machine Number (M/C No.)
    """
    data_list = state['input_data']
    if not data_list:
         return {"processed_data": pd.DataFrame()}
         
    rows = []
    for data in data_list:
        piece = float(data.get('Piece', 0))
        processed_piece = piece / 3.0
        
        row = {
            'ORDER DATE': data.get('Order Date'),
            'M/C No.': data.get('M/C No.'),
            'DESIGN NO': data.get('Design No'),
            'BEAM': data.get('Beam'),
            'RATE': data.get('Rate'),
            'PIECE (Divided by 3)': processed_piece
        }
        
        # Add all the feeder data dynamically
        feeders = data.get('Feeders', {})
        for k, v in feeders.items():
            row[k] = v
            
        rows.append(row)
        
    df = pd.DataFrame(rows)
    
    # Sort by M/C No to group machines together for the schedule
    df['M/C No.'] = df['M/C No.'].astype(str).str.strip()
    df = df.sort_values(by=['M/C No.', 'DESIGN NO'])
    df = df.reset_index(drop=True)
    
    return {"processed_data": df}

def build_workflow():
    """Builds and returns the compiled LangGraph workflow"""
    workflow = StateGraph(GraphState)
    
    workflow.add_node("process", process_node)
    
    workflow.set_entry_point("process")
    workflow.add_edge("process", END)
    
    return workflow.compile()
