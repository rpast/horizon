import os
import io
import re
import time
import openai
import datetime
import ast
import pandas as pd

from flask import Flask, request, session, render_template, redirect, url_for, jsonify, send_file
from langchain.document_loaders import PyPDFLoader

## Local modules import
from chatbot import Chatbot
import params as prm
import cont_proc as cproc
from db_handler import DatabaseHandler
import oai_tool as oai

# Serve app to prod
import webbrowser
from waitress import serve
from threading import Timer

##########################################################################################

# Set up paths
template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)),"static")


# Initiate Flask app
app = Flask(
    __name__, 
    template_folder=template_folder,
    static_folder=static_folder
    )

app.secret_key = os.urandom(24)

###########################################################################################

# Render home page
@app.route('/')
def home():
    return render_template(
        'home.html'
        )

@app.route('/collection_manager', methods=['GET', 'POST'])
def collection_manager():
    # TODO: fetch collections from database and pass them so they can be displayed
    return render_template(
            'collection_manager.html'
            )

# create /process_collection route
@app.route('/process_collection', methods=['POST'])
def process_collection():
    """Process collection
    Process the collection of documents.
    """
    print("!Processing collection")

    # Get the data from the form
    collection_name = request.form['collection_name']
    collection_name = cproc.process_name(collection_name)
    print(f"!Collection name: {collection_name}")

    # Process the collection
    file_ = request.files['pdf']
    file_name = cproc.process_name(file_.filename)
    collection_source = file_name
    print(f"!Collection source: {collection_source}")

    # Save the file to the upload folder
    saved_fname = collection_name + '_' + file_name
    fpath = os.path.join(prm.UPLOAD_FOLDER, saved_fname)
    file_.save(fpath)
    print(f"!File saved to: {fpath}")

    # Load the pdf & process the text
    loader = PyPDFLoader(fpath) # langchain simple pdf loader
    pages = loader.load_and_split() # split by pages

    # Process text data further so it fits the context mechanism
    pages_df = cproc.pages_to_dataframe(pages)
    pages_refined_df = cproc.split_pages(pages_df)
    pages_processed_df = cproc.prepare_for_embed(pages_refined_df, collection_name)

    # Add UUIDs to the dataframe!
    pages_processed_df['uuid'] = cproc.create_uuid()
    pages_processed_df['doc_uuid'] = [cproc.create_uuid() for x in range(pages_processed_df.shape[0])]


    # TODO: Switch to Hugging Face API with embedding model
    # Get the embedding cost
    embedding_cost = round(cproc.embed_cost(pages_processed_df),4)
    # express embedding cost in dollars
    embedding_cost = f"${embedding_cost}"
    doc_length = pages_processed_df.shape[0]
    length_warning = doc_length / 60 > 1
    print(f"!Embedding cost: {embedding_cost}")

    if length_warning != True:
        # Perform the embedding process here
        print('Embedding process started...')
        pages_embed_df = cproc.embed_pages(pages_processed_df)
        print('Embedding process finished.')
        ## TODO: use vectorstore to store embeddings
        pages_embed_df['embedding'] = pages_embed_df['embedding'].astype(str)

        ## DMODEL UPDATE
        ## TODO: Decouple context from embeddings
        to_serialize_df = pages_embed_df[['name', 'embedding']]
        embed_df = cproc.serialize_embedding(to_serialize_df)
        #######################

        # insert data with embedding to main context table with if exist = append.
        with db as db_conn:
            db_conn.insert_context(pages_embed_df)
        
        print('!Embedding process finished. Collection saved to database.')
        
    return render_template(
            'collection_manager.html'
            )

# Create /session_manager route
@app.route('/session_manager', methods=['GET', 'POST'])
def session_manager():
    """Session manager
    Manage sessions.
    """
    
    # Load session names from the database
    with db as db_conn:
        # We want to see available sessions
        if db_conn.load_session_names() is not None:
            session_names = [x[0] for x in db_conn.load_session_names()]
            session_ids = [x[1] for x in db_conn.load_session_names()]

            # extract from session dates only the date YYYY-MM-DD
            # session_dates = [x.split()[0] for x in session_dates]

            sessions = list(zip(session_names, session_ids))
        else:
            sessions = []
        
        # We want to see available collections
        collections = db_conn.load_collections_all()

    return render_template(
            'session_manager.html',
            sessions=sessions,
            collections=collections
            )

#Create /process_session
@app.route('/process_session', methods=['POST'])
def process_session():
    """Process session
    Set the API key, session name, connect sources for new session.
    """

    session['UUID'] = cproc.create_uuid()
    session['SESSION_DATE'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    ## Get the data from the form
    # Pass API key right to the openai object
    # openai.api_key = request.form['api_key']

    #determine if use clicked session_create or session_start
    session_action = request.form.get('session_action', 0)

    # Determine if we deal with new or existing session
    # And handle session variables accordingly

    if session_action == 'Start':
        name_grabbed = request.form.getlist('existing_session_name')
        sesion_id = [ast.literal_eval(x)[1] for x in name_grabbed][0]
        name = [ast.literal_eval(x)[0] for x in name_grabbed][0]
        print('Starting existing session: ', name)
        session['SESSION_NAME'] = name
        session['UUID'] = sesion_id

        return redirect(
            url_for('index'))


    elif session_action == 'Create':
        session_name = request.form.get('new_session_name', 0)
        session['SESSION_NAME'] = cproc.process_name(session_name)
        print('Creating new session: ', session['SESSION_NAME'])

        # grab collections from the form
        collections = request.form.getlist('collections')

        collection_ids = [ast.literal_eval(x)[0] for x in collections]
        print('Collections: ', collection_ids)
        for collection_uuid in collection_ids:
            with db as db_conn:
                db_conn.insert_session(
                    session['UUID'],
                    collection_uuid,
                    session['SESSION_NAME'],
                    session['SESSION_DATE']
                )

        return redirect(
            url_for('index'))


@app.route('/interaction')
def index():
    """render interaction main page
    """

    # Load chat history
    with db as db_conn:
        chat_history = db_conn.load_context(
            session['UUID'], 
            table_name='chat_history'
            )

    # If chat history is empty it means this is the first interaction
    # we need to insert the baseline exchange   
    if chat_history.empty:    
        #insert baseline interaction
        with db as db_conn:
            db_conn.insert_interaction(
                session['UUID'],
                'user',
                prm.SUMMARY_CTXT_USR
            )
            db_conn.insert_interaction(
                session['UUID'],
                'assistant',
                prm.SUMMARY_TXT_ASST
            )
    else:
        # Remove seed interactions from the chat history
        chat_history = chat_history[chat_history['timestamp'] != 0]

    # Convert the DataFrame to a JSON object so we can pass it to the template
    chat_history_json = chat_history.to_dict(orient='records')

    return render_template(
        'index.html',
        session_name=session['SESSION_NAME'],
        session_date=session['SESSION_DATE'],
        session_uuid=session['UUID'],
        chat_history=chat_history_json
        )


@app.route('/ask', methods=['POST'])
def ask():
    """handle POST request from the form and return the response
    """

    data = request.get_json()
    question = data['question']

    # Handle chat memory and context
    print('Handling chat memory and context...')
    
    with db as db_conn:
        # Form recall tables
        collections = db_conn.load_collections(session['UUID'])
        recall_table_context = db_conn.load_context(collections)
        recall_table_chat = db_conn.load_context(session['UUID'], table_name='chat_history')
    
    recall_table_source = recall_table_context
    recall_table_user, recall_table_assistant = cproc.prepare_chat_recall(recall_table_chat)

    recal_embed_source = cproc.convert_table_to_dct(recall_table_source)
    recal_embed_user = cproc.convert_table_to_dct(recall_table_user)
    recal_embed_assistant = cproc.convert_table_to_dct(recall_table_assistant)


    ## Get the context from recall table that is the most similar to user input
    num_samples = prm.NUM_SAMPLES
    if recall_table_source.shape[0] < prm.NUM_SAMPLES:
        # This should happen for short documents otherwise this suggests a bug (usually with session name)
        num_samples = recall_table_source.shape[0]
        print('WARNING! Source material is shorter than number of samples you want to get. Setting number of samples to the number of source material sections.')


    # Get the closest index - This will update index attributes of chatbot object
    # that are used later to retrieve text and page numbers

    chatbot.retrieve_closest_idx(
        question,
        num_samples,
        recal_embed_source,
        recal_embed_user,
        recal_embed_assistant
    )

    recal_source, recal_user, recal_agent = chatbot.retrieve_text(
        recall_table_context,
        recall_table_chat,
    )

    # Look for agent and user messages in the interaction table that have the latest timestamp
    # We will put them in the context too.
    last_usr_max = recall_table_user['timestamp'].astype(int).max()
    last_asst_max = recall_table_assistant['timestamp'].astype(int).max()
    if last_usr_max == 0:
        latest_user = 'No context found'
    else:
        latest_user = recall_table_user[recall_table_user['timestamp']==last_usr_max]['text'].values[0]

    if last_asst_max == 0:
        latest_assistant = 'No context found'
    else:
        latest_assistant = recall_table_assistant[recall_table_assistant['timestamp']==last_asst_max]['text'].values[0]

    print('Done handling chat memory and context.')
    
    ## Grab the page number from the recall table
    ## It will become handy when user wants to know from which chapter the context was taken

    if len(chatbot.recall_source_idx)>1:
        recall_source_pages = recall_table_context.loc[chatbot.recall_source_idx]['page'].to_list()
    elif len(chatbot.recall_source_idx)==1:
        recall_source_pages = recall_table_context.loc[chatbot.recall_source_idx]['page']
    else:
        recall_source_pages = 'No context found'

    # print(f'I will answer your question basing on the following context: {set(recall_source_pages)}')
    # print('\n')
    # print('Prompt build: ')
    # print('Latest user message: ', latest_user)
    # print('Latest assistant message: ', latest_assistant)
    # print('Recall source: ', recal_source)
    # print('Recall user: ', recal_user)
    # print('Recall agent: ', recal_agent)
    # print('Question: ', question)
    # print('\n')
    # Build prompt
    message = chatbot.build_prompt(
        latest_user,
        latest_assistant,
        recal_source,
        recal_user,
        recal_agent,
        question
        )
    print("!Prompt built")


    # Grab call user content from messages alias
    usr_message_content = message[0]['content']

    # Count number of tokens in user message and display it to the user
    # TODO: flash it on the front-end
    token_passed = oai.num_tokens_from_messages(message)
    context_capacity =  4096 - token_passed
    print(f"Number of tokens passed to the model: {token_passed}")
    print(f"Number of tokens left in the context: {context_capacity}")


    # generate response
    response = chatbot.chat_completion_response(message)
    print("!Response generated")


    # save it all to DB so the agent can remember the conversation
    session['SPOT_TIME'] = str(int(time.time()))
    with db as db_conn:
        # Insert user message into DB so we can use it for another user's input
        db_conn.insert_interaction(
            session['UUID'],
            'user',
            question,
            timestamp=session['SPOT_TIME']
        )
        db_conn.insert_interaction(
            session['UUID'],
            'assistant',
            response['choices'][0]['message']['content'],
            timestamp=response['created']
        )

    return jsonify({'response': response})


@app.route('/export_interactions', methods=['GET'])
def export_interactions():
    """Export the interaction table as a JSON file for download.
    """

    # Connect to the database
    with db as db_conn:
        # Retrieve the interaction table
        recall_df = db_conn.load_context(session['UUID'], table_name='chat_history')

    # remove records that are user or assistant interaction type and have 
    # time signature 0 - these were injected into the table as a seed to 
    # improve performance of the model at the beginning of the conversation
    seed_f = (
        (recall_df['interaction_type'].isin(['user','assistant'])) & (recall_df['timestamp'] == 0)
        )
    recall_df = recall_df[~seed_f]

    # Convert the DataFrame to a JSON string
    interactions_json = recall_df.to_json(orient='records', indent=2)

    # Create a file-like buffer to hold the JSON string
    json_buffer = io.BytesIO()
    json_buffer.write(interactions_json.encode('utf-8'))
    json_buffer.seek(0)

    # Send the JSON file to the user for download
    return send_file(
        json_buffer, 
        as_attachment=True, 
        download_name=f"interactions_{session['SESSION_NAME']}.json", 
        mimetype='application/json')


def open_browser():
    """Open default browser to display the app in PROD mode
    """
    webbrowser.open_new('http://127.0.0.1:5000/')



if __name__ == '__main__':

    ## Load key from api_key.txt THIS IS FOR DEV ONLY
    with open('/home/nf/Documents/projekty/ai_apps/ALP/ALP/static/data/api_key.txt') as f:
        key_ = f.read()
        openai.api_key = key_


    # Intitiate database if not exist
    db_exist = os.path.exists(prm.DB_PATH)
    print(f'Database exists: {db_exist}')
    if not db_exist:
        # Initialize the database
        db = DatabaseHandler(prm.DB_PATH)
        with db as db_conn:
            db_conn.write_db(prm.SESSION_TABLE_SQL)
            # db.write_db(prm.INTERIM_COLLECTIONS_TABLE_SQL)
            db_conn.write_db(prm.COLLECTIONS_TABLE_SQL)
            db_conn.write_db(prm.CHAT_HIST_TABLE_SQL)
            db_conn.write_db(prm.EMBEDDINGS_TABLE_SQL)
    else:
        # Initialize the database
        db = DatabaseHandler(prm.DB_PATH)

    # Spin up chatbot instance
    chatbot = Chatbot()
    print("!Chatbot initialized")


    # Run DEV server
    app.run(debug=True, host='0.0.0.0', port=5000)

    # run PROD server
    # Timer(1, open_browser).start()
    # serve(app, host='0.0.0.0', port=5000)