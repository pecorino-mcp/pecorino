"""
Hotspot detection module for Gitstats3.

Identifies high-risk code areas by combining:
- Churn frequency (files with frequent modifications)
- Complexity score (cyclomatic + cognitive complexity)
- Change coupling (files that always change together)
- Risk score calculation
"""

from collections import defaultdict
from typing import Dict, List, Tuple, Optional, Any

from .gitstats_config import conf
from .gitstats_gitcommands import getpipeoutput


class HotspotDetector:
    """
    Detects code hotspots by analyzing file churn, complexity, and coupling.
    
    Hotspots are files that have both high change frequency AND high complexity,
    making them high-risk areas for bugs and maintenance issues.
    """
    
    def __init__(self, data_collector):
        """
        Initialize the hotspot detector.
        
        Args:
            data_collector: A DataCollector instance with collected repository data
        """
        self.data = data_collector
        self.hotspots: List[Dict[str, Any]] = []
        self.change_coupling: Dict[str, List[Tuple[str, float]]] = {}
        self.file_churn: Dict[str, Dict[str, Any]] = {}
        self.file_complexity: Dict[str, Dict[str, Any]] = {}
    
    def analyze(self) -> List[Dict[str, Any]]:
        """
        Run full hotspot analysis and return ranked hotspots.
        
        Returns:
            List of hotspot dictionaries sorted by risk score (highest first)
        """
        if conf.get('verbose'):
            print('Analyzing code hotspots...')
        
        self._calculate_churn()
        self._calculate_complexity()
        self._detect_change_coupling()
        self._calculate_risk_scores()
        
        return self.hotspots
    
    def _calculate_churn(self):
        """Calculate file churn metrics from revision history."""
        # Get file revisions from data collector (use property directly)
        file_revisions = self.data.file_revisions
        
        # Calculate churn statistics
        if file_revisions:
            total_revisions = sum(file_revisions.values())
            max_revisions = max(file_revisions.values()) if file_revisions else 1
            
            for filepath, revisions in file_revisions.items():
                # Normalize churn score (0-100)
                churn_score = (revisions / max_revisions) * 100 if max_revisions > 0 else 0
                
                self.file_churn[filepath] = {
                    'revisions': revisions,
                    'churn_score': churn_score,
                    'relative_churn': (revisions / total_revisions * 100) if total_revisions > 0 else 0
                }
    
    def _calculate_complexity(self):
        """Get complexity metrics for each file."""
        # Get file metrics from comprehensive_metrics
        comprehensive_metrics = getattr(self.data, 'comprehensive_metrics', {})
        file_metrics = comprehensive_metrics.get('file_metrics', {})
        
        if not file_metrics:
            # Fallback to code_analysis if comprehensive_metrics not available
            file_metrics = self.data.code_analysis.get('file_metrics', {})
        
        for filepath, metrics in file_metrics.items():
            if isinstance(metrics, dict):
                mi_data = metrics.get('maintainability_index', {})
                complexity_data = metrics.get('complexity', {})
                
                # Get maintainability index (lower = more complex)
                mi = mi_data.get('mi', 100) if isinstance(mi_data, dict) else 100
                
                # Get cyclomatic complexity
                cyclomatic = complexity_data.get('cyclomatic', 0) if isinstance(complexity_data, dict) else 0
                
                # Use cached complexity_score if available, otherwise calculate
                if 'complexity_score' in metrics:
                    combined_complexity = metrics['complexity_score']
                else:
                    # Fallback: Calculate complexity score from MI and cyclomatic
                    complexity_score = max(0, 100 - (mi / 171 * 100))
                    combined_complexity = (complexity_score * 0.6) + (min(cyclomatic * 2, 100) * 0.4)
                
                self.file_complexity[filepath] = {
                    'maintainability_index': mi,
                    'cyclomatic_complexity': cyclomatic,
                    'complexity_score': combined_complexity,
                    'interpretation': mi_data.get('interpretation', 'unknown') if isinstance(mi_data, dict) else 'unknown'
                }
    
    def _detect_change_coupling(self):
        """Detect files that frequently change together (temporal coupling)."""
        try:
            # Get commit log with changed files
            log_output = getpipeoutput([
                'git log --name-only --pretty=format:"COMMIT:%H" --no-merges -n 500'
            ])
            
            # Parse commits and their files
            commits = []
            current_files = []
            
            for line in log_output.split('\n'):
                line = line.strip()
                if line.startswith('COMMIT:'):
                    if current_files:
                        commits.append(current_files)
                    current_files = []
                elif line:
                    current_files.append(line)
            
            if current_files:
                commits.append(current_files)
            
            # Count co-occurrences
            co_occurrence: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
            file_commit_count: Dict[str, int] = defaultdict(int)
            
            for files in commits:
                unique_files = set(files)
                for f in unique_files:
                    file_commit_count[f] += 1
                
                # Count pairs
                file_list = list(unique_files)
                for i, file1 in enumerate(file_list):
                    for file2 in file_list[i+1:]:
                        co_occurrence[file1][file2] += 1
                        co_occurrence[file2][file1] += 1
            
            # Calculate coupling strength for each file
            for filepath in self.file_churn.keys():
                coupled_files = []
                if filepath in co_occurrence:
                    my_count = file_commit_count.get(filepath, 1)
                    
                    for other_file, co_count in co_occurrence[filepath].items():
                        # Coupling strength = co-occurrence / min(individual occurrences)
                        other_count = file_commit_count.get(other_file, 1)
                        coupling_strength = co_count / min(my_count, other_count)
                        
                        if coupling_strength > 0.3:  # Only track significant coupling
                            coupled_files.append((other_file, coupling_strength))
                    
                    # Sort by coupling strength
                    coupled_files.sort(key=lambda x: x[1], reverse=True)
                
                self.change_coupling[filepath] = coupled_files[:5]  # Top 5 coupled files
                
        except Exception as e:
            if conf.get('debug'):
                print(f'Warning: Change coupling detection failed: {e}')
    
    def _calculate_risk_scores(self):
        """Calculate final risk scores and rank hotspots."""
        all_files = set(self.file_churn.keys()) | set(self.file_complexity.keys())
        
        for filepath in all_files:
            churn_info = self.file_churn.get(filepath, {})
            complexity_info = self.file_complexity.get(filepath, {})
            coupling_info = self.change_coupling.get(filepath, [])
            
            churn_score = churn_info.get('churn_score', 0)
            complexity_score = complexity_info.get('complexity_score', 0)
            
            # Coupling penalty: more coupled files = higher risk
            coupling_penalty = min(len(coupling_info) * 5, 25)  # Max 25 points
            
            # Risk formula: (churn × complexity) / 100 + coupling penalty
            # This prioritizes files that are BOTH frequently changed AND complex
            risk_score = (churn_score * complexity_score) / 100 + coupling_penalty
            
            # Classify risk level
            if risk_score >= 60:
                risk_level = 'critical'
            elif risk_score >= 40:
                risk_level = 'high'
            elif risk_score >= 20:
                risk_level = 'medium'
            else:
                risk_level = 'low'
            
            self.hotspots.append({
                'filepath': filepath,
                'risk_score': round(risk_score, 2),
                'risk_level': risk_level,
                'churn_score': round(churn_score, 2),
                'complexity_score': round(complexity_score, 2),
                'revisions': churn_info.get('revisions', 0),
                'maintainability_index': complexity_info.get('maintainability_index', None),
                'cyclomatic_complexity': complexity_info.get('cyclomatic_complexity', 0),
                'coupled_files': coupling_info,
                'coupling_count': len(coupling_info)
            })
        
        # Sort by risk score (highest first)
        self.hotspots.sort(key=lambda x: x['risk_score'], reverse=True)
    
    def get_hotspots(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Get top hotspots by risk score.
        
        Args:
            limit: Maximum number of hotspots to return
            
        Returns:
            List of hotspot dictionaries
        """
        return self.hotspots[:limit]
    
    def get_hotspots_by_level(self, level: str) -> List[Dict[str, Any]]:
        """
        Get hotspots filtered by risk level.
        
        Args:
            level: Risk level ('critical', 'high', 'medium', 'low')
            
        Returns:
            List of hotspot dictionaries matching the level
        """
        return [h for h in self.hotspots if h['risk_level'] == level]
    
    def get_summary(self) -> Dict[str, Any]:
        """
        Get a summary of hotspot analysis.
        
        Returns:
            Dictionary with summary statistics
        """
        critical = len([h for h in self.hotspots if h['risk_level'] == 'critical'])
        high = len([h for h in self.hotspots if h['risk_level'] == 'high'])
        medium = len([h for h in self.hotspots if h['risk_level'] == 'medium'])
        low = len([h for h in self.hotspots if h['risk_level'] == 'low'])
        
        return {
            'total_files_analyzed': len(self.hotspots),
            'critical_hotspots': critical,
            'high_risk_hotspots': high,
            'medium_risk_hotspots': medium,
            'low_risk_hotspots': low,
            'top_hotspots': self.get_hotspots(5),
            'files_with_coupling': len([h for h in self.hotspots if h['coupling_count'] > 0])
        }


def analyze_hotspots(data_collector, limit: int = 20) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Convenience function to run hotspot analysis.
    
    Args:
        data_collector: A DataCollector instance
        limit: Maximum number of hotspots to return
        
    Returns:
        Tuple of (hotspots list, summary dict)
    """
    detector = HotspotDetector(data_collector)
    detector.analyze()
    return detector.get_hotspots(limit), detector.get_summary()
