#!/usr/bin/env python3


from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import math
import os
import sys
import random
import zipfile

import numpy as np
from six.moves import urllib
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf


bloom_filter_max_size = 65536
num_hash_fun = 7

def read_data(filename):
    """Extract the first file enclosed in a zip file as a list of words."""
    with open(filename) as f:
        res = []
        data = tf.compat.as_str(f.read()).split()
        for word in data:
            word_idx_list = [int(idx) for idx in word.split(',')]
            res.append(tuple(sorted(word_idx_list)))
    return res


vocabulary = read_data(sys.argv[1])
print('Data size', len(vocabulary))

# Step 2: Build the dictionary and replace rare words with UNK token.


def build_dataset(words):
    """Process raw inputs into a dataset."""

    count = []
    rank_matrix = []
    words_counter = collections.Counter(words)
    count.extend(words_counter.most_common(len(words_counter)))
    dictionary = dict()
    for word, _ in count:
        dictionary[word] = len(dictionary)
        
    reversed_dictionary = dict(zip(dictionary.values(), dictionary.keys()))
    for i in range(len(count)):
        rank_matrix.append(list(reversed_dictionary[i]))
    return count, dictionary, reversed_dictionary, rank_matrix


count, dictionary, reverse_dictionary, rank_matrix = build_dataset(vocabulary)
vocabulary_size = len(dictionary)

print('Most common words (+UNK)', count[:5])
data_index = 0

# Step 3: Function to generate a training batch for the skip-gram model.


vocabulary = [list(v) for v in vocabulary]

print('len(vocabulary):', len(vocabulary))

def generate_batch(batch_size, num_skips, skip_window):
    global data_index
    assert batch_size % num_skips == 0
    assert num_skips <= 2 * skip_window
    batch = np.ndarray(shape=(batch_size, num_hash_fun), dtype=np.int32)
    labels = np.ndarray(shape=(batch_size, 1), dtype=np.int32)
    span = 2 * skip_window + 1  # [ skip_window target skip_window ]
    buffer = collections.deque(maxlen=span)
    for _ in range(span):
        buffer.append(vocabulary[data_index])
        data_index = (data_index + 1) % len(vocabulary)
    for i in range(batch_size // num_skips):
        target = skip_window  # target label at the center of the buffer
        targets_to_avoid = [skip_window]
        for j in range(num_skips):
            while target in targets_to_avoid:
                target = random.randint(0, span - 1)
            targets_to_avoid.append(target)
            batch[i * num_skips + j] = buffer[skip_window]
            labels[i * num_skips + j, 0] = dictionary[tuple(buffer[target])]
        buffer.append(vocabulary[data_index])
        data_index = (data_index + 1) % len(vocabulary)
    # Backtrack a little bit to avoid skipping words in the end of a batch
    data_index = (data_index + len(vocabulary) - span) % len(vocabulary)
    return batch, labels


batch, labels = generate_batch(batch_size=8, num_skips=2, skip_window=1)
print(labels)
for i in range(8):
    print(batch[i], dictionary[tuple(batch[i])],
          '->', labels[i], reverse_dictionary[labels[i, 0]])

# Step 4: Build and train a skip-gram model.

batch_size = 130
embedding_size = 128  # Dimension of the embedding vector.
skip_window = 1       # How many words to consider left and right.
num_skips = 2         # How many times to reuse an input to generate a label.

# We pick a random validation set to sample nearest neighbors. Here we limit the
# validation samples to the words that have a low numeric ID, which by
# construction are also the most frequent.
valid_size = 16     # Random set of words to evaluate similarity on.
valid_window = 100  # Only pick dev samples in the head of the distribution.
valid_examples = np.random.choice(valid_window, valid_size, replace=False)
num_sampled = 64    # Number of negative examples to sample.

graph = tf.Graph()

with graph.as_default():

    # Input data.
    train_inputs = tf.placeholder(tf.int32, shape=[batch_size, num_hash_fun])
    train_labels = tf.placeholder(tf.int32, shape=[batch_size, 1])
    valid_dataset = tf.constant(valid_examples, dtype=tf.int32)
    rank_matrix = tf.stack(rank_matrix)

    # Ops and variables pinned to the CPU because of missing GPU implementation
    with tf.device('/cpu:0'):
        print('train_inputs.shape = ', train_inputs.shape)
        # Look up embeddings for inputs.
        embeddings = tf.Variable(
            tf.random_uniform([bloom_filter_max_size, embedding_size], -1.0, 1.0))
        print('embeddings.shape = ', embeddings.shape)
        embed = tf.nn.embedding_lookup(embeddings, train_inputs)
        print('embed.shape = ', embed.shape)
        embed = tf.reduce_mean(embed, 1)
        print('embed.shape = ', embed.shape)
        
        

        # Construct the variables for the NCE loss
        nce_weights = tf.Variable(
            tf.truncated_normal([bloom_filter_max_size, embedding_size],
                                stddev=1.0 / math.sqrt(embedding_size)))
        nce_biases = tf.Variable(tf.zeros([bloom_filter_max_size]))

    # Compute the average NCE loss for the batch.
    # tf.nce_loss automatically draws a new sample of the negative labels each
    # time we evaluate the loss.

    loss = tf.reduce_mean(
        tf.nn.nce_loss(weights=nce_weights,
                       biases=nce_biases,
                       labels=train_labels,
                       inputs=embed,
                       num_sampled=num_sampled,
                       num_classes=vocabulary_size,
                       rank_matrix=rank_matrix))

    # Construct the SGD optimizer using a learning rate of 1.0.
    optimizer = tf.train.GradientDescentOptimizer(1.0).minimize(loss)

    # Compute the cosine similarity between minibatch examples and all embeddings.
    norm = tf.sqrt(tf.reduce_sum(tf.square(embeddings), 1, keep_dims=True))
    normalized_embeddings = embeddings / norm
    valid_dataset_indice = tf.nn.embedding_lookup(rank_matrix, valid_dataset)
    valid_embeddings = tf.nn.embedding_lookup(
        normalized_embeddings, valid_dataset_indice)
    valid_embeddings = tf.reduce_mean(valid_embeddings, 1)

    all_words_embeddings = tf.nn.embedding_lookup(normalized_embeddings, rank_matrix)
    all_words_embeddings = tf.reduce_mean(all_words_embeddings, 1)

    similarity = tf.matmul(
        valid_embeddings, all_words_embeddings, transpose_b=True)
    print('similarity.shape = ', similarity.shape)
    # Add variable initializer.
    init = tf.global_variables_initializer()

# Step 5: Begin training.
num_steps = 100001



with tf.Session(graph=graph) as session:
    # We must initialize all variables before we use them.
    init.run()
    print('Initialized')

    saver = tf.train.Saver()

    average_loss = 0
    for step in xrange(num_steps):
        batch_inputs, batch_labels = generate_batch(
            batch_size, num_skips, skip_window)
        feed_dict = {train_inputs: batch_inputs, train_labels: batch_labels}

        # We perform one update step by evaluating the optimizer op (including it
        # in the list of returned values for session.run()
        _, loss_val = session.run([optimizer, loss], feed_dict=feed_dict)
        # my_labels = session.run(train_labels, feed_dict=feed_dict)
        # print('feed_dict: {}'.format(feed_dict))
        # print('Vec: {}'.format(my_labels))
        average_loss += loss_val

        if step % 2000 == 0:
            if step > 0:
                average_loss /= 2000
            # The average loss is an estimate of the loss over the last 2000 batches.
            print('Average loss at step ', step, ': ', average_loss)
            average_loss = 0

        # Note that this is expensive (~20% slowdown if computed every 500 steps)
        '''
        if step % 10000 == 0:
            sim = similarity.eval()
            for i in xrange(valid_size):
                valid_word = reverse_dictionary[valid_examples[i]]
                top_k = 8  # number of nearest neighbors
                nearest = (-sim[i, :]).argsort()[1:top_k + 1]
                log_str = 'Nearest to {}: '.format(valid_word)
                for k in xrange(top_k):
                    close_word = reverse_dictionary[nearest[k]]
                    log_str = '%s %s,' % (log_str, close_word)
                print(log_str)
        '''

    final_embeddings = normalized_embeddings.eval()
    save_path = saver.save(session, "model.ckpt")

# Step 6: Visualize the embeddings.


def plot_with_labels(low_dim_embs, labels, filename='tsne.png'):
    assert low_dim_embs.shape[0] >= len(labels), 'More labels than embeddings'
    plt.figure(figsize=(18, 18))  # in inches
    for i, label in enumerate(labels):
        x, y = low_dim_embs[i, :]
        plt.scatter(x, y)
        plt.annotate(label,
                     xy=(x, y),
                     xytext=(5, 2),
                     textcoords='offset points',
                     ha='right',
                     va='bottom')

    plt.savefig(filename)


try:
    # pylint: disable=g-import-not-at-top
    from sklearn.manifold import TSNE
    import matplotlib.pyplot as plt

    tsne = TSNE(perplexity=30, n_components=2, init='pca', n_iter=5000)
    plot_only = 500
    low_dim_embs = tsne.fit_transform(final_embeddings[:plot_only, :])
    labels = [reverse_dictionary[i] for i in xrange(plot_only)]
    plot_with_labels(low_dim_embs, labels)

except ImportError:
    print('Please install sklearn, matplotlib, and scipy to show embeddings.')
