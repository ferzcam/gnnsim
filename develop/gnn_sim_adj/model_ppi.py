from mowl.model import Model
from mowl.graph.util import parser_factory
import pandas as pd
import click as ck
import os
import pickle as pkl
import logging
import dgl
from dgl import nn as dglnn
from dgl.dataloading import GraphDataLoader
import torch as th
import numpy as np
from math import floor
from torch import nn
from torch.nn import functional as F
from torch import optim
from .baseRGCN import BaseRGCN
from sklearn.metrics import roc_curve, auc, matthews_corrcoef
from torch.utils.data import DataLoader, IterableDataset
#from dgl.nn.pytorch import RelGraphConv
from .rgcnConv import RelGraphConv

from mowl.graph.edge import Edge

import random
from ray import tune

class GNNSimPPI(Model):
    def __init__(self,
                 dataset,
                 n_hidden,
                 dropout,
                 learning_rate,
                 num_bases,
                 batch_size,
                 epochs,
                 use_case,
                 graph_generation_method = "taxonomy", #Default: generate graph taxonomy
                 normalize = False,
                 regularization = 0,
                 self_loop = False,
                 min_edges = 0, #Only takes the relation types in which the number of triples is greater than min_edges. If 0 then takes all the relation types
                 seed = -1,
                 file_params = None #Dictionary of data file paths corresponding to the graph generation method (NEEDS REFACTORING)
                 ):
        super().__init__(dataset)
        self.n_hidden = n_hidden
        self.dropout = dropout
        self.learning_rate = learning_rate
        self.num_bases = None if num_bases < 0 else num_bases
        self.batch_size =  batch_size
        self.epochs = epochs
        self.use_case = use_case
        self.graph_generation_method = graph_generation_method
        self.normalize = normalize
        self.regularization = regularization
        self.self_loop = self_loop
        self.min_edges = min_edges
        self.file_params = file_params
        
        if seed>=0:
            th.manual_seed(seed)
            np.random.seed(seed)
            random.seed(seed)

        self.test_df = None
        self.g = None
        self.annots = None
        self.prot_idx = None
        self.num_rels = None
        self.adj_mat = None

    def train(self, checkpoint_dir = None, tuning= False):
        
        logging.info("Loading graph data...")
        g, annots, prot_idx, num_rels, adj_mat = self.load_graph_data()
        
        logging.info(f"PPI use case: Num nodes: {g.number_of_nodes()}")
        logging.info(f"PPI use case: Num edges: {g.number_of_edges()}")
        train_df, val_df, test_df = self.load_ppi_data()

        annots = th.FloatTensor(annots)

        self.test_df = test_df
        self.g = g
        self.annots = annots
        self.prot_idx = prot_idx
        self.num_rels = num_rels
        self.adj_mat = th.tensor(adj_mat, device = "cuda")

        train_labels = th.FloatTensor(train_df['labels'].values)
        val_labels = th.FloatTensor(val_df['labels'].values)
    
        train_data = GraphDatasetPPI(g, train_df, train_labels, annots, prot_idx)
        val_data = GraphDatasetPPI(g, val_df, val_labels, annots, prot_idx)

        train_dataloader = GraphDataLoader(train_data, batch_size = self.batch_size, drop_last = True)
        val_dataloader = GraphDataLoader(val_data, batch_size = self.batch_size, drop_last = True)

        if self.num_bases is None:
            self.num_bases = num_rels
        
        num_nodes = g.number_of_nodes()

        device = "cuda"
        model = PPIModel(num_rels, self.num_bases, num_nodes, self.n_hidden, self.dropout, self.adj_mat).to(device)
        print(model)

        optimizer = optim.Adam(model.parameters(), lr=self.learning_rate, weight_decay=self.regularization)
        
        if checkpoint_dir:
            model_state, optimizer_state = th.load(
                os.path.join(checkpoint_dir, "checkpoint"))
            model.load_state_dict(model_state)
            optimizer.load_state_dict(optimizer_state)

        loss_func = nn.BCELoss()

        early_stopping_limit = 3
        early_stopping = early_stopping_limit
        best_loss = float("inf")
        best_roc_auc = 0
        early_stopping_limit = 3
        best_loss = float("inf")
        for epoch in range(self.epochs):
            epoch_loss = 0
            model.train()

            with ck.progressbar(train_dataloader) as bar:
            
                for i, (batch_g, batch_labels, feats1, feats2) in enumerate(bar):

                    feats1 = annots[feats1]#.view(-1,1)
                    #print("Feature size ", feats1.shape)
                    
                    feats2 = annots[feats2]#.view(-1,1)
                    
                    logits = model(batch_g.to(device), feats1.to(device), feats2.to(device)).squeeze()

                    labels = batch_labels.squeeze().to(device)
                    loss = loss_func(logits, labels.float())
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()
                    epoch_loss += loss.detach().item()

                epoch_loss /= (i+1)
        
            model.eval()
            val_loss = 0
            preds = []
            labels = []
            with th.no_grad():
                optimizer.zero_grad()
                with ck.progressbar(val_dataloader) as bar:
                    for i, (batch_g, batch_labels, feats1, feats2) in enumerate(bar):
                        
                        feats1 = annots[feats1]#.view(-1,1)
                        feats2 = annots[feats2]#.view(-1,1)
                        
                        logits = model(batch_g.to(device), feats1.to(device), feats2.to(device)).squeeze()
                        lbls = batch_labels.squeeze().to(device)
                        loss = loss_func(logits, lbls.float())
                        val_loss += loss.detach().item()
                        labels = np.append(labels, lbls.cpu())
                        preds = np.append(preds, logits.cpu())
                    val_loss /= (i+1)

            roc_auc = self.compute_roc(labels, preds)
            if not tuning:

                if val_loss <= best_loss:
                    best_loss = val_loss

                    th.save(model.state_dict(), self.file_params["output_model"])
                if val_loss < best_loss:
                    best_loss = val_loss
                    early_stopping = early_stopping_limit
                else:
                    early_stopping -= 1
                print(f'Epoch {epoch}: Loss - {epoch_loss}, \tVal loss - {val_loss}, \tAUC - {roc_auc}')
                
                if early_stopping == 0:
                    print("Finished training (early stopping)")
                    break

            else:
                with tune.checkpoint_dir(epoch) as checkpoint_dir:
                    path = os.path.join(checkpoint_dir, "checkpoint")
                    th.save((model.state_dict(), optimizer.state_dict()), path)

                tune.report(loss=(val_loss), auc=roc_auc)
        print("Finished Training")
        

    def evaluate(self, tuning=False, best_checkpoint_dir=None):

        device = "cpu"

        if self.test_df is None:
            _, _, test_df = self.load_pp_data()
        else:
            test_df = self.test_df
        g = self.g
        annots = self.annots
        prot_idx = self.prot_idx
        num_rels = self.num_rels
        
        test_labels = th.FloatTensor(test_df['labels'].values)
            
        test_data = GraphDatasetPPI(g, test_df, test_labels, annots, prot_idx)

        test_dataloader = GraphDataLoader(test_data, batch_size = self.batch_size, drop_last = True)

        num_nodes = g.number_of_nodes()

        if self.num_bases is None:
            self.num_bases = num_rels

        loss_func = nn.BCELoss()

        model = PPIModel(num_rels, self.num_bases, num_nodes, self.n_hidden, self.dropout, self.adj_mat.cpu())

        if tuning:
            model_state, optimizer_state = th.load(os.path.join(best_checkpoint_dir, "checkpoint"))
            model.load_state_dict(model_state)
 
        else:
            model.load_state_dict(th.load(self.file_params["output_model"]))
        
        model.to(device)
        model.eval()
        test_loss = 0

        preds = []
        all_labels = []
        with th.no_grad():
            with ck.progressbar(test_dataloader) as bar:
                for iter, (batch_g, batch_labels, feats1, feats2) in enumerate(bar):
                    feats1 = annots[feats1]#.view(-1,1)
                    feats2 = annots[feats2]#.view(-1,1)
                    
                    logits = model(batch_g.to(device), feats1.to(device), feats2.to(device)).squeeze()
                    labels = batch_labels.squeeze().to(device)
                    loss = loss_func(logits, labels.float())
                    test_loss += loss.detach().item()
                    preds = np.append(preds, logits.cpu())
                    all_labels = np.append(all_labels, labels.cpu())
                test_loss /= (iter+1)

        roc_auc = self.compute_roc(all_labels, preds)
        print(f'Test loss - {test_loss}, \tAUC - {roc_auc}')

        return test_loss, roc_auc

    def compute_roc(self, labels, preds):
    # Compute ROC curve and ROC area for each class
        fpr, tpr, _ = roc_curve(labels.flatten(), preds.flatten())
        roc_auc = auc(fpr, tpr)

        return roc_auc

    def load_ppi_data(self):
        train_df = pd.read_pickle(self.file_params["train_inter_file"])
        valid_df = pd.read_pickle(self.file_params["valid_inter_file"])
        test_df = pd.read_pickle(self.file_params["test_inter_file"])
        logging.info("Original ata sizes: Train %d, Val %d, Test %d", len(train_df), len(valid_df), len(test_df))

        index = np.arange(len(train_df))
        np.random.seed(seed=0)
        np.random.shuffle(index)
        train_df = train_df.iloc[index]#[:20000]]

        index = np.arange(len(valid_df))
        np.random.seed(seed=0)
        np.random.shuffle(index)
        valid_df = valid_df.iloc[index]#[:5000]]
        
        index = np.arange(len(test_df))
        np.random.seed(seed=0)
        np.random.shuffle(index)
        test_df = test_df.iloc[index]#[:5000]]

        logging.info("Used data sizes: Train %d, Val %d, Test %d", len(train_df), len(valid_df), len(test_df))
        return train_df, valid_df, test_df

    def load_graph_data(self):

        logging.debug("Creating graph generation method...")
        parser = parser_factory(self.graph_generation_method, self.dataset.ontology, bidirectional_taxonomy = True)
        edges_path = f"data/edges_{self.use_case}_{self.graph_generation_method}.pkl"
        logging.debug("Created")
        try:
            logging.debug("try")
            infile = open(edges_path, 'rb')
            edges = pkl.load(infile)
        except:
            logging.debug("except")
            logging.debug("Parsing ontology...")
            edges = parser.parse()
            logging.info("Finished parsing ontology")
            edges = [(str(e.src()), str(e.rel()), str(e.dst())) for e in edges]
            outfile = open(edges_path, 'wb')
            pkl.dump(edges, outfile)

        terms_file = self.file_params["terms_file"]

        with open(terms_file) as f:
            terms = f.read().splitlines()
            edges = [(s, r, d) for s,r,d in edges if s in terms and  d in terms]
        

        srcs, rels, dsts = tuple(map(list, zip(*edges)))
        

        g = dgl.DGLGraph()
        nodes = list(set(srcs).union(set(dsts)))
        g.add_nodes(len(nodes))
        node_idx = {v: k for k, v in enumerate(nodes)}
        
        if self.self_loop:
            srcs += nodes
            dsts += nodes
            rels += ["id" for _ in range(len(nodes))]

            
        srcs = [node_idx[s] for s in srcs]
        dsts = [node_idx[d] for d in dsts]
        
        edges_per_rel = {}
        for rel in rels:
            if not rel in edges_per_rel:
                edges_per_rel[rel] = 0
            edges_per_rel[rel] += 1

            

        if self.normalize:
            edge_node = {}
            rels_dst = list(zip(rels, dsts))

            for rel, dst in rels_dst:
                if (rel, dst) not in edge_node:
                    edge_node[(rel, dst)] = 0
                edge_node[(rel, dst)] += 1
            
            edge_node = {k: 1/v for k, v in edge_node.items()}
            
            norm = [edge_node[i] for i in rels_dst]


            zipped_data = list(zip(srcs, dsts, rels, norm))
            srcs, dsts, rels, norm = zip(*[(s, d, r, n) for s, d, r, n in zipped_data if edges_per_rel[r] > self.min_edges])

            norm = th.Tensor(norm).view(-1, 1)

            
        else:
            norm = None

            zipped_data = list(zip(srcs, dsts, rels))
            srcs, dsts, rels = zip(*[(s, d, r) for s, d, r in zipped_data if edges_per_rel[r] > self.min_edges])

            
        rels_idx = {v: k for k, v in enumerate(set(rels))}
        rels = [rels_idx[r] for r in rels]
        num_rels = len(rels_idx)
        rels = th.Tensor(rels)

        
        print("Edges in graph:\n", edges_per_rel)
        g.add_edges(srcs, dsts)


        g.edata.update({'rel_type': rels})
        if norm != None:
            g.edata.update({'norm': norm})

        
        num_nodes = g.number_of_nodes()
          
        adj_mat = g.adj(scipy_fmt="csr").toarray()
        print(adj_mat.shape, num_nodes)
        assert adj_mat.shape == (num_nodes, num_nodes)

        
        data_file = self.file_params["data_file"]

        prot_idx = {}
        with open(data_file, 'r') as f:
            rows = [line.strip('\n').split('\t') for line in f.readlines()]

            annotations = np.zeros((len(rows), num_nodes), dtype=np.float32)

            for i, row  in enumerate(rows):
                prot_id = row[0]
                prot_idx[prot_id] = i
                for go_id in row[1:]:
                    if go_id in node_idx:
                        annotations[i, node_idx[go_id]] = 1
        return g, annotations, prot_idx, num_rels, adj_mat



    
class RGCN(BaseRGCN):

         def build_hidden_layer(self, idx):
             act = F.relu if idx < self.num_hidden_layers - 1 else None
             return RelGraphConv(self.h_dim, self.h_dim, self.num_rels, "basis",
                self.num_bases, activation=act, self_loop=True,
                dropout=self.dropout)


class FLinear(nn.Module):

    def __init__(self, in_dim, out_dim):
        super().__init__()
        
        self.fc = nn.Linear(in_dim, out_dim)

    def forward(self, x, adj_mat):
        x = th.matmul(x, th.mul(self.fc.weight, adj_mat)) + self.fc.bias
        return x
         
class PPIModel(nn.Module):

    def __init__(self, num_rels, num_bases, num_nodes, n_hid, dropout, adj_mat):
        super().__init__()

        self.h_dim = 1
        self.num_rels = num_rels
        self.num_bases = None if num_bases < 0 else num_bases
        self.num_nodes = num_nodes
        self.adj_mat = adj_mat.repeat((2,2))

        print(f"Num rels: {self.num_rels}")
        print(f"Num bases: {self.num_bases}")


        dim = 2*num_nodes
        self.fc1 = FLinear(dim, dim)
        self.fc2 = FLinear(dim, dim)
        self.fc3 = FLinear(dim, dim)
        #self.fc2 = nn.Linear(2*num_nodes, 2*num_nodes)
        self.out = nn.Linear(dim, 1)
        self.dropout = nn.Dropout()
        dim1 = self.num_nodes
        dim2 = floor(self.num_nodes/2)

        self.net = nn.Sequential(
            nn.Linear(dim1, 1024)
#            nn.Dropout(),
#            nn.Linear(dim2, 1024),
#            nn.Dropout()
        )

    def forward_each(self, g, features, edge_type, norm):
        initial = features
        
        #        print("features: ", features.shape, self.fc.weight.shape, self.adj_mat.shape)
       
        #        x = th.matmul(features, th.mul(self.fc.weight, self.adj_mat)) + self.fc.bias
        #        x = self.fc(features)
        x = self.fc1(features, self.adj_mat)
        x = F.relu(x)
        x = self.fc2(features, self.adj_mat)
        return self.dropout(x)

    def forward(self, g, feat1, feat2):

        edge_type = g.edata['rel_type'].long()

        norm = None if not 'norm' in g.edata else g.edata['norm']
        
        # x1 = self.forward_each(g, feat1, edge_type, norm)
        # x2 = self.forward_each(g, feat2, edge_type, norm)
        feats = th.cat([feat1, feat2], dim=1)
 
        x = self.fc1(feats, self.adj_mat)
        x = F.relu(x)
        x = self.out(x)

#        x = th.sum(x1 * x2, dim=1, keepdims=True)
#        x = th.cat([x1, x2]).view(-1,2048)
        return th.sigmoid(x)


    

class GraphDatasetPPI(IterableDataset):

    def __init__(self, g, df, labels, annots, prot_idx):
        self.graph = g
        self.annots = annots
        self.labels = labels
        self.df = df
        self.prot_idx = prot_idx

    def get_data(self):
        for i, row in enumerate(self.df.itertuples()):
            p1, p2 = row.interactions
            label = self.labels[i].view(1, 1)
            if p1 not in self.prot_idx or p2 not in self.prot_idx:
                continue
            pi1, pi2 = self.prot_idx[p1], self.prot_idx[p2]

            feat1 = self.annots[:, pi1]
            feat2 = self.annots[:, pi2]

            yield (self.graph, label, pi1, pi2)

    def __iter__(self):
        return self.get_data()

    def __len__(self):
        return len(self.df)


def node_norm_to_edge_norm(g, node_norm):
    g = g.local_var()
    # convert to edge norm
    g.ndata['norm'] = node_norm
    g.apply_edges(lambda edges : {'norm' : edges.dst['norm']})
    return g.edata['norm']
