'''
Script to process debug SGFs for upload to BigQuery.

Handles one generation per invocation, for easy sharding of work.

Usage:
python oneoffs/prepare_bigquery.py 000017-generation-number


The load commands look like:
bq load --project_id=$PROJECT_ID \
    --source_format=NEWLINE_DELIMITED_JSON \
    $PROJECT_ID:minigo_v5_19.games \
    gs://$BUCKET_NAME/bigquery/holdout/games/* \
    oneoffs/bigquery_games_schema.json

bq load --project_id=$PROJECT_ID \
    --source_format=NEWLINE_DELIMITED_JSON \
    $PROJECT_ID:minigo_v5_19.moves \
    gs://$BUCKET_NAME/bigquery/holdout/moves/* \
    oneoffs/bigquery_moves_schema.json

'''
import collections
import itertools
import json
import os
import re
import sys; sys.path.insert(0, '.')
import sgf
from tensorflow import gfile

import coords
import shipname
import sgf_wrapper
import utils

DebugRow = collections.namedtuple('DebugRow', [
    'move', 'action', 'Q', 'U', 'prior', 'orig_prior', 'N', 'soft_N', 'p_delta', 'p_rel'])

BUCKET_NAME = os.environ['BUCKET_NAME']
GCS_HOLDOUT_PATH = 'gs://%s/data/holdout/' % BUCKET_NAME
GCS_PATH_TEMPLATE = 'gs://%s/sgf/{}/full/{}' % BUCKET_NAME
OUTPUT_PATH_TEMPLATE = 'gs://%s/bigquery/holdout/{}/{}' % BUCKET_NAME

def extract_holdout_generation(generation_name):
    tfrecord_paths = gfile.ListDirectory(GCS_HOLDOUT_PATH + generation_name)
    filenames = [os.path.basename(path) for path in tfrecord_paths]
    sgf_names = [GCS_PATH_TEMPLATE.format(generation_name, filename.replace('.tfrecord.zz', '.sgf'))
        for filename in filenames]
    game_output_path = OUTPUT_PATH_TEMPLATE.format('games', generation_name)
    move_output_path = OUTPUT_PATH_TEMPLATE.format('moves', generation_name)
    with gfile.GFile(game_output_path, 'w') as game_f:
        with gfile.GFile(move_output_path, 'w') as move_f:
            for sgf_name in sgf_names:
                game_data, move_data = extract_data(sgf_name)
                game_f.write(json.dumps(game_data))
                game_f.write('\n')
                for move_datum in move_data:
                    move_f.write(json.dumps(move_datum))
                    move_f.write('\n')
                print('processed {}'.format(sgf_name))


def extract_data(filename):
    with gfile.GFile(filename) as f:
        contents = f.read()
        root_node = sgf_wrapper.get_sgf_root_node(contents)
    game_data = extract_game_data(filename, root_node)
    move_data = extract_move_data(
        root_node, game_data['worker_id'], game_data['completed_time'],
        game_data['board_size'])
    return game_data, move_data

def extract_game_data(gcs_path, root_node):
    props = root_node.properties
    komi = float(sgf_wrapper.sgf_prop(props.get('KM')))
    result = sgf_wrapper.sgf_prop(props.get('RE', ''))
    board_size = int(sgf_wrapper.sgf_prop(props.get('SZ')))
    value = utils.parse_game_result(result)
    was_resign = '+R' in result
    
    filename = os.path.basename(gcs_path)
    filename_no_ext, _ = os.path.splitext(filename)
    completion_time = int(filename_no_ext.split('-')[0])
    worker_id = filename_no_ext.split('-')[-1]
    model_num = shipname.detect_model_num(props.get('PW')[0])
    sgf_url = gcs_path
    first_comment_node_lines = root_node.next.properties['C'][0].split('\n')
    # in-place edit to comment node so that first move's comment looks
    # the same as all the other moves.
    root_node.next.properties['C'][0] = '\n'.join(first_comment_node_lines[1:])
    resign_threshold = float(first_comment_node_lines[0].split()[-1])

    return {
        'worker_id': worker_id,
        'completed_time': completion_time * 1000, # BigQuery's TIMESTAMP() takes in unix millis.
        'board_size': board_size,
        'model_num': model_num,
        'result_str': result,
        'value': value,
        'was_resign': was_resign,
        'sgf_url': sgf_url,
        'resign_threshold': resign_threshold,
    }

def extract_move_data(root_node, worker_id, completed_time, board_size):
    current_node = root_node.next
    move_data = []
    move_num = 1
    while current_node is not None:
        props = current_node.properties
        if 'B' in props:
            to_play = 1
            move_played = props['B'][0]
        elif 'W' in props:
            to_play = -1
            move_played = props['W'][0]
        else:
            import pdb; pdb.set_trace()
        move_played = coords.to_flat(coords.from_sgf(move_played))
        post_Q, debug_rows = parse_comment_node(props['C'][0])
        policy_prior = [0] * (board_size * board_size + 1)
        policy_prior_orig = policy_prior[:]
        mcts_visit_counts = policy_prior[:]
        mcts_visit_counts_norm = policy_prior[:]
        for debug_row in debug_rows:
            move = debug_row.move
            policy_prior[move] = debug_row.prior
            policy_prior_orig[move] = debug_row.orig_prior
            mcts_visit_counts[move] = debug_row.N
            mcts_visit_counts_norm[move] = debug_row.soft_N

        move_data.append({
            'worker_id': worker_id,
            'completed_time': completed_time,
            'move_num': move_num,
            'turn_to_play': to_play,
            'move': move_played,
            'move_kgs': coords.to_kgs(coords.from_flat(move_played)),
            'prior_Q': None,
            'post_Q': post_Q, 
            'policy_prior': policy_prior,
            'policy_prior_orig': policy_prior_orig,
            'mcts_visit_counts': mcts_visit_counts,
            'mcts_visit_counts_norm': mcts_visit_counts_norm,
        })
        move_num += 1
        current_node = current_node.next
    return move_data


def parse_comment_node(comment):
    # Example of a comment node. The resign threshold line appears only
    # for the first move in the game; it gets preprocessed by extract_game_data
    """
    Resign Threshold: -0.88
    -0.0662
    D4 (100) ==> D16 (14) ==> Q16 (3) ==> Q4 (1) ==> Q: -0.07149
    move: action Q U P P-Dir N soft-N p-delta p-rel
    D4 : -0.028, -0.048, 0.020, 0.048, 0.064, 100 0.1096 0.06127 1.27
    D16 : -0.024, -0.043, 0.019, 0.044, 0.059, 96 0.1053 0.06135 1.40
    """

    lines = comment.split('\n')
    if lines[0].startswith('Resign'):
        lines = lines[1:]

    post_Q = float(lines[0])
    debug_rows = []
    for line in lines[3:]:
        if not line: continue
        columns = re.split(r'[ :,]', line)
        columns = list(filter(bool, columns))
        coord, *other_columns = columns
        coord = coords.to_flat(coords.from_kgs(coord))
        debug_rows.append(DebugRow(coord, *map(float, other_columns)))
    return post_Q, debug_rows

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("Usage: python {} 000017-generation-name")
        sys.exit(1)
    generation_name = sys.argv[1]
    extract_holdout_generation(generation_name)