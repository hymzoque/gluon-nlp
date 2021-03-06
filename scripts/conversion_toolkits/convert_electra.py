import os
import re
import sys
import shutil
import logging
import argparse

import mxnet as mx
import numpy as np
from numpy.testing import assert_allclose

from gluonnlp.utils.misc import naming_convention, logging_config
from gluonnlp.data.tokenizers import HuggingFaceWordPieceTokenizer
from gluonnlp.models.electra import ElectraModel, \
    ElectraGenerator, ElectraDiscriminator, ElectraForPretrain, get_generator_cfg
import tensorflow.compat.v1 as tf

tf.disable_eager_execution()
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '1'

mx.npx.set_np()
np.random.seed(1234)
mx.npx.random.seed(1234)


def parse_args():
    parser = argparse.ArgumentParser(description='Convert the TF Electra Model to Gluon')
    parser.add_argument('--tf_model_path', type=str,
                        help='Directory of the model downloaded from TF hub.')
    parser.add_argument('--electra_path', type=str,
                        help='Path to the github repository of electra, you may clone it by '
                             '`git clone https://github.com/ZheyuYe/electra.git`.')
    parser.add_argument('--model_size', type=str, choices=['small', 'base', 'large'],
                        help='Size of the Electra model')
    parser.add_argument('--save_dir', type=str, default=None,
                        help='directory path to save the converted Electra model.')
    parser.add_argument('--gpu', type=int, default=None,
                        help='a single gpu to run mxnet, e.g. 0 or 1 The default device is cpu ')
    parser.add_argument('--test', action='store_true')
    args = parser.parse_args()
    return args


def read_tf_checkpoint(path):
    """read tensorflow checkpoint"""
    from tensorflow.python import pywrap_tensorflow
    tensors = {}
    reader = pywrap_tensorflow.NewCheckpointReader(path)
    var_to_shape_map = reader.get_variable_to_shape_map()
    for key in sorted(var_to_shape_map):
        tensor = reader.get_tensor(key)
        tensors[key] = tensor
    return tensors


def get_dict_config(model_size, electra_path):
    sys.path.append(electra_path)
    electra_dir = os.path.abspath(os.path.join(os.path.dirname(electra_path), os.path.pardir))
    sys.path.append(electra_dir)
    from electra.util.training_utils import get_bert_config
    from electra.configure_pretraining import PretrainingConfig

    config = PretrainingConfig(model_name='', data_dir='', model_size=model_size)
    bert_config = get_bert_config(config)
    # we are not store all configuration of electra generators but only the scale size.
    config_dict = bert_config.to_dict()
    config_dict.update(
        {'embedding_size': config.embedding_size,
         'generator_hidden_size': config.generator_hidden_size,
         'generator_layers': config.generator_layers,
         })
    return config_dict


def convert_tf_config(config_dict, vocab_size):
    """Convert the config file"""

    assert vocab_size == config_dict['vocab_size']
    cfg = ElectraModel.get_cfg().clone()
    cfg.defrost()
    cfg.MODEL.vocab_size = vocab_size
    cfg.MODEL.units = config_dict['hidden_size']
    cfg.MODEL.embed_size = config_dict['embedding_size']
    cfg.MODEL.hidden_size = config_dict['intermediate_size']
    cfg.MODEL.max_length = config_dict['max_position_embeddings']
    cfg.MODEL.num_heads = config_dict['num_attention_heads']
    cfg.MODEL.num_layers = config_dict['num_hidden_layers']
    cfg.MODEL.pos_embed_type = 'learned'
    cfg.MODEL.activation = config_dict['hidden_act']
    cfg.MODEL.layer_norm_eps = 1E-12
    cfg.MODEL.num_token_types = config_dict['type_vocab_size']
    cfg.MODEL.hidden_dropout_prob = float(config_dict['hidden_dropout_prob'])
    cfg.MODEL.attention_dropout_prob = float(config_dict['attention_probs_dropout_prob'])
    cfg.MODEL.dtype = 'float32'
    cfg.MODEL.generator_layers_scale = config_dict['generator_layers']
    cfg.MODEL.generator_units_scale = config_dict['generator_hidden_size']
    cfg.INITIALIZER.weight = ['truncnorm', 0,
                              config_dict['initializer_range']]  # TruncNorm(0, 0.02)
    cfg.INITIALIZER.bias = ['zeros']
    cfg.VERSION = 1
    cfg.freeze()
    return cfg


def convert_tf_assets(tf_assets_dir, model_size, electra_path):
    """Convert the assets file including config, vocab and tokenizer model"""
    file_names = os.listdir(tf_assets_dir)
    vocab_path = None
    for ele in file_names:
        if ele.endswith('.txt'):
            assert vocab_path is None
            vocab_path = ele
    assert vocab_path is not None

    if vocab_path:
        vocab_path = os.path.join(tf_assets_dir, vocab_path)
        vocab_size = len(open(vocab_path, 'r', encoding='utf-8').readlines())
    config_dict = get_dict_config(model_size, electra_path)
    cfg = convert_tf_config(config_dict, vocab_size)
    return cfg, vocab_path


CONVERT_MAP = [
    ('backbone_model.discriminator_predictions/dense_1', 'rtd_encoder.2'),
    ('backbone_model.discriminator_predictions/dense', 'rtd_encoder.0'),
    ('backbone_model.generator_predictions/dense', 'mlm_decoder.0'),
    ('backbone_model.generator_predictions/LayerNorm', 'mlm_decoder.2'),
    ('backbone_model.generator_predictions/output_bias', 'mlm_decoder.3.bias'),
    ('electra/', ''),
    ('generator/', ''),
    ('embeddings_project', 'embed_factorized_proj'),
    ('embeddings/word_embeddings', 'word_embed.weight'),
    ('embeddings/token_type_embeddings', 'token_type_embed.weight'),
    ('embeddings/position_embeddings', 'token_pos_embed._embed.weight'),
    ('layer_', 'all_encoder_layers.'),
    ('embeddings/LayerNorm', 'embed_layer_norm'),
    ('attention/output/LayerNorm', 'layer_norm'),
    ('attention/output/dense', 'attention_proj'),
    ('output/LayerNorm', 'ffn.layer_norm'),
    ('LayerNorm', 'layer_norm'),
    ('intermediate/dense', 'ffn.ffn_1'),
    ('output/dense', 'ffn.ffn_2'),
    ('output/', ''),
    ('kernel', 'weight'),
    ('/', '.'),
]


def get_name_map(tf_names, convert_type='backbone'):
    """
    Get the converting mapping between tensor names and mxnet names.
    The above mapping CONVERT_MAP is effectively adaptive to Bert and Albert,
    but there is no guarantee that it can match to other tf models in case of
    some sepecial variable_scope (tensorflow) and prefix (mxnet).

    Redefined mapping is encouraged to adapt the personalization model.

    Parameters
    ----------
    tf_names
        the parameters names of tensorflow model
    convert_type
        choices=['backbone', 'disc', 'gen']
    Returns
    -------
    A dictionary with the following format:
        {tf_names : mx_names}
    """
    name_map = {}
    for source_name in tf_names:
        target_name = source_name
        if convert_type == 'backbone':
            if 'electra' not in source_name:
                continue
        elif convert_type == 'disc':
            target_name = 'backbone_model.' + target_name
            if 'generator' in source_name:
                continue
        elif convert_type == 'gen':
            target_name = 'backbone_model.' + target_name
            if 'generator' not in source_name:
                continue
        else:
            raise NotImplementedError
        # skip the qkv weights
        if 'self/' in source_name:
            name_map[source_name] = None
            continue
        for old, new in CONVERT_MAP:
            target_name = target_name.replace(old, new)
        name_map[source_name] = target_name
    return name_map


def convert_tf_model(model_dir, save_dir, test_conversion, model_size, gpu, electra_path):
    ctx = mx.gpu(gpu) if gpu is not None else mx.cpu()
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    cfg, vocab_path = convert_tf_assets(model_dir, model_size, electra_path)
    with open(os.path.join(save_dir, 'model.yml'), 'w') as of:
        of.write(cfg.dump())
    new_vocab = HuggingFaceWordPieceTokenizer(
        vocab_file=vocab_path,
        unk_token='[UNK]',
        pad_token='[PAD]',
        cls_token='[CLS]',
        sep_token='[SEP]',
        mask_token='[MASK]',
        lowercase=True).vocab
    new_vocab.save(os.path.join(save_dir, 'vocab.json'))

    # test input data
    batch_size = 3
    seq_length = 32
    num_mask = 5
    input_ids = np.random.randint(0, cfg.MODEL.vocab_size, (batch_size, seq_length))
    valid_length = np.random.randint(seq_length // 2, seq_length, (batch_size,))
    input_mask = np.broadcast_to(np.arange(seq_length).reshape(1, -1), (batch_size, seq_length)) \
        < np.expand_dims(valid_length, 1)
    segment_ids = np.random.randint(0, 2, (batch_size, seq_length))
    mlm_positions = np.random.randint(0, seq_length // 2, (batch_size, num_mask))

    tf_input_ids = tf.constant(input_ids, dtype=np.int32)
    tf_input_mask = tf.constant(input_mask, dtype=np.int32)
    tf_segment_ids = tf.constant(segment_ids, dtype=np.int32)

    init_checkpoint = os.path.join(model_dir, 'electra_{}'.format(model_size))
    tf_params = read_tf_checkpoint(init_checkpoint)
    # get parameter names for tensorflow with unused parameters filtered out.
    tf_names = sorted(tf_params.keys())
    tf_names = filter(lambda name: not name.endswith('adam_m'), tf_names)
    tf_names = filter(lambda name: not name.endswith('adam_v'), tf_names)
    tf_names = filter(lambda name: name != 'global_step', tf_names)
    tf_names = filter(lambda name: name != 'generator_predictions/temperature', tf_names)
    tf_names = list(tf_names)

    # reload the electra module for this local scope
    sys.path.append(electra_path)
    electra_dir = os.path.abspath(os.path.join(os.path.dirname(electra_path), os.path.pardir))
    sys.path.append(electra_dir)
    from electra.util.training_utils import get_bert_config
    from electra.configure_pretraining import PretrainingConfig
    from electra.model import modeling

    config = PretrainingConfig(model_name='', data_dir='', model_size=model_size)
    bert_config = get_bert_config(config)
    bert_model = modeling.BertModel(
        bert_config=bert_config,
        is_training=False,
        input_ids=tf_input_ids,
        input_mask=tf_input_mask,
        token_type_ids=tf_segment_ids,
        use_one_hot_embeddings=False,
        embedding_size=cfg.MODEL.embed_size)
    tvars = tf.trainable_variables()
    assignment_map, _ = modeling.get_assignment_map_from_checkpoint(tvars, init_checkpoint)
    tf.train.init_from_checkpoint(init_checkpoint, assignment_map)

    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        # the name of the parameters are ending with ':0' like
        # 'electra/embeddings/word_embeddings:0'
        backbone_params = {v.name.split(":")[0]: v.read_value() for v in tvars}
        backbone_params = sess.run(backbone_params)
        tf_token_outputs_np = {
            'pooled_output': sess.run(bert_model.get_pooled_output()),
            'sequence_output': sess.run(bert_model.get_sequence_output()),
        }

    # The following part only ensure the parameters in backbone model are valid
    for k in backbone_params:
        assert_allclose(tf_params[k], backbone_params[k])

    # Build gluon model and initialize
    gluon_model = ElectraModel.from_cfg(cfg)
    gluon_model.initialize(ctx=ctx)
    gluon_model.hybridize()

    gluon_disc_model = ElectraDiscriminator(cfg)
    gluon_disc_model.initialize(ctx=ctx)
    gluon_disc_model.hybridize()

    gen_cfg = get_generator_cfg(cfg)
    disc_backbone = gluon_disc_model.backbone_model
    gluon_gen_model = ElectraGenerator(gen_cfg)
    gluon_gen_model.tie_embeddings(disc_backbone.word_embed.collect_params(),
                                   disc_backbone.token_type_embed.collect_params(),
                                   disc_backbone.token_pos_embed.collect_params(),
                                   disc_backbone.embed_layer_norm.collect_params())
    gluon_gen_model.initialize(ctx=ctx)
    gluon_gen_model.hybridize()

    # pepare test data
    mx_input_ids = mx.np.array(input_ids, dtype=np.int32, ctx=ctx)
    mx_valid_length = mx.np.array(valid_length, dtype=np.int32, ctx=ctx)
    mx_token_types = mx.np.array(segment_ids, dtype=np.int32, ctx=ctx)
    mx_masked_positions = mx.np.array(mlm_positions, dtype=np.int32, ctx=ctx)

    for convert_type in ['backbone', 'disc', 'gen']:
        name_map = get_name_map(tf_names, convert_type=convert_type)
        # go through the gluon model to infer the shape of parameters

        if convert_type == 'backbone':
            model = gluon_model
            contextual_embedding, pooled_output = model(
                mx_input_ids, mx_token_types, mx_valid_length)
        elif convert_type == 'disc':
            model = gluon_disc_model
            contextual_embedding, pooled_output, rtd_scores = \
                model(mx_input_ids, mx_token_types, mx_valid_length)
        elif convert_type == 'gen':
            model = gluon_gen_model
            contextual_embedding, pooled_output, mlm_scores = \
                model(mx_input_ids, mx_token_types, mx_valid_length, mx_masked_positions)

        # replace tensorflow parameter names with gluon parameter names
        mx_params = model.collect_params()
        all_keys = set(mx_params.keys())
        for (src_name, dst_name) in name_map.items():
            tf_param_val = tf_params[src_name]
            if dst_name is None:
                continue
            all_keys.remove(dst_name)
            if src_name.endswith('kernel'):
                mx_params[dst_name].set_data(tf_param_val.T)
            else:
                mx_params[dst_name].set_data(tf_param_val)

        # Merge query/kernel, key/kernel, value/kernel to encoder.all_encoder_groups.0.attn_qkv.weight
        def convert_qkv_weights(tf_prefix, mx_prefix):
            """
            To convert the qkv weights with different prefix.

            In tensorflow framework, the prefix of query/key/value for the albert model is
            'bert/encoder/transformer/group_0/inner_group_0/attention_1/self/query/kernel',
            and that for the bert model is 'bert/encoder/layer_{}/attention/self/key/bias'.
            In gluonnlp framework, the prefix is slightly different as
            'encoder.all_encoder_groups.0.attn_qkv.weight' for albert model and
            'encoder.all_layers.{}.attn_qkv.weight' for bert model, as the
            curly braces {} can be filled with the layer number.
            """
            # Merge query_weight, key_weight, value_weight to mx_params
            query_weight = tf_params[
                '{}/query/kernel'.format(tf_prefix)]
            key_weight = tf_params[
                '{}/key/kernel'.format(tf_prefix)]
            value_weight = tf_params[
                '{}/value/kernel'.format(tf_prefix)]
            mx_params['{}.attn_qkv.weight'.format(mx_prefix)].set_data(
                np.concatenate([query_weight, key_weight, value_weight], axis=1).T)
            # Merge query_bias, key_bias, value_bias to mx_params
            query_bias = tf_params[
                '{}/query/bias'.format(tf_prefix)]
            key_bias = tf_params[
                '{}/key/bias'.format(tf_prefix)]
            value_bias = tf_params[
                '{}/value/bias'.format(tf_prefix)]
            mx_params['{}.attn_qkv.bias'.format(mx_prefix)].set_data(
                np.concatenate([query_bias, key_bias, value_bias], axis=0))

        # The below parameters of the generator are already initialized in the
        # discriminator, no need to reload.
        disc_embed_params = set(['backbone_model.embed_layer_norm.beta',
                                 'backbone_model.embed_layer_norm.gamma',
                                 'backbone_model.token_pos_embed._embed.weight',
                                 'backbone_model.token_type_embed.weight',
                                 'mlm_decoder.3.weight',
                                 'backbone_model.word_embed.weight'])

        for key in all_keys:
            if convert_type == 'gen' and key in disc_embed_params:
                continue
            assert re.match(r'^(backbone_model\.){0,1}encoder\.all_encoder_layers\.[\d]+\.attn_qkv\.(weight|bias)$',
                            key) is not None, 'Parameter key {} mismatch'.format(key)

        tf_prefix = None
        for layer_id in range(cfg.MODEL.num_layers):
            mx_prefix = 'encoder.all_encoder_layers.{}'.format(layer_id)
            if convert_type == 'gen':
                mx_prefix = 'backbone_model.' + mx_prefix
                tf_prefix = 'generator/encoder/layer_{}/attention/self'.format(layer_id)
            elif convert_type == 'disc':
                mx_prefix = 'backbone_model.' + mx_prefix
                tf_prefix = 'electra/encoder/layer_{}/attention/self'.format(layer_id)
            else:
                tf_prefix = 'electra/encoder/layer_{}/attention/self'.format(layer_id)

            convert_qkv_weights(tf_prefix, mx_prefix)

        if convert_type == 'backbone':
            # test conversion results for backbone model
            if test_conversion:
                tf_contextual_embedding = tf_token_outputs_np['sequence_output']
                tf_pooled_output = tf_token_outputs_np['pooled_output']
                contextual_embedding, pooled_output = model(
                    mx_input_ids, mx_token_types, mx_valid_length)
                assert_allclose(pooled_output.asnumpy(), tf_pooled_output, 1E-3, 1E-3)
                for i in range(batch_size):
                    ele_valid_length = valid_length[i]
                    assert_allclose(contextual_embedding[i, :ele_valid_length, :].asnumpy(),
                                    tf_contextual_embedding[i, :ele_valid_length, :], 1E-3, 1E-3)
            model.save_parameters(os.path.join(save_dir, 'model.params'), deduplicate=True)
            logging.info('Convert the backbone model in {} to {}/{}'.format(model_dir,
                                                                            save_dir, 'model.params'))
        elif convert_type == 'disc':
            model.save_parameters(os.path.join(save_dir, 'disc_model.params'), deduplicate=True)
            logging.info(
                'Convert the discriminator model in {} to {}/{}'.format(model_dir, save_dir, 'disc_model.params'))
        elif convert_type == 'gen':
            model.save_parameters(os.path.join(save_dir, 'gen_model.params'), deduplicate=True)
            logging.info('Convert the generator model in {} to {}/{}'.format(model_dir,
                                                                             save_dir, 'gen_model.params'))

    logging.info('Conversion finished!')
    logging.info('Statistics:')

    old_names = os.listdir(save_dir)
    for old_name in old_names:
        new_name, long_hash = naming_convention(save_dir, old_name)
        old_path = os.path.join(save_dir, old_name)
        new_path = os.path.join(save_dir, new_name)
        shutil.move(old_path, new_path)
        file_size = os.path.getsize(new_path)
        logging.info('\t{}/{} {} {}'.format(save_dir, new_name, long_hash, file_size))


if __name__ == '__main__':
    args = parse_args()
    logging_config()
    save_dir = args.save_dir if args.save_dir is not None else os.path.basename(
        args.tf_model_path) + '_gluon'
    convert_tf_model(
        args.tf_model_path,
        save_dir,
        args.test,
        args.model_size,
        args.gpu,
        args.electra_path)
